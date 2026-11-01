"""Reads the laptop sensors on a schedule without ever being held up by one of them.

Each sensor gets its own loop. Once a second the loop starts a read **in a separate
thread** (a thread is a second line of execution; if it gets stuck, the rest of the
program carries on) and waits at most one second for the answer:

* an answer arrives in time: the reading is ``available`` with that value;
* the read fails, returns nothing, or returns something that is not a number: the
  reading is ``unavailable`` with value ``None``;
* no answer in one second (the sensor is hung): ``unavailable``, and the loop moves on.

The previous number is **never** reused: a reading is either fresh or ``unavailable``.
A late answer from a read we gave up on is thrown away. A sensor that is still stuck
from an earlier read is not asked again (so stuck threads cannot pile up) and shows
``unavailable`` until that read finishes.

Every 10 seconds the available readings are saved as gauge metrics
(``laptop_cpu_load`` and so on) through the metrics recorder.

The newest readings are kept in memory and returned by ``readings()``; the dashboard
reads them from there, never from the database.
"""

import asyncio
import concurrent.futures
import math
import threading
from collections.abc import Awaitable, Callable, Sequence

from deterrent.clock import Clock, elapsed_ms
from deterrent.health.sensors import AVAILABLE, UNAVAILABLE, HealthReading, SensorProvider
from deterrent.observability.metrics import MetricsRecorder

INTERVAL_S = 1.0
TIMEOUT_S = 1.0
PERSIST_S = 10.0

Spawn = Callable[[Callable[[], float | None]], "concurrent.futures.Future"]


def spawn_daemon_thread(work: Callable[[], float | None]) -> concurrent.futures.Future:
    """Run ``work`` in its own daemon thread (it cannot keep the program from exiting)."""
    future: concurrent.futures.Future = concurrent.futures.Future()
    future.set_running_or_notify_cancel()

    def run() -> None:
        try:
            future.set_result(work())
        except BaseException as error:
            future.set_exception(error)

    threading.Thread(target=run, name="sensor-read", daemon=True).start()
    return future


def _as_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


class HealthCollector:
    def __init__(
        self,
        providers: Sequence[SensorProvider],
        clock: Clock,
        clock_us: Callable[[], int],
        sleep: Callable[[float], Awaitable[None]],
        metrics: MetricsRecorder | None,
        *,
        interval_s: float = INTERVAL_S,
        timeout_s: float = TIMEOUT_S,
        persist_s: float = PERSIST_S,
        spawn: Spawn = spawn_daemon_thread,
    ) -> None:
        names = [p.name for p in providers]
        if len(set(names)) != len(names):
            raise ValueError("each sensor name can appear only once")
        self._providers = list(providers)
        self._clock = clock
        self._clock_us = clock_us
        self._sleep = sleep
        self._metrics = metrics
        self._interval_s = interval_s
        self._timeout_s = timeout_s
        self._persist_s = persist_s
        self._spawn = spawn
        self._inflight: dict[str, concurrent.futures.Future] = {}
        self._readings: dict[str, HealthReading] = {
            p.name: HealthReading(p.name, None, p.unit, UNAVAILABLE, 0) for p in providers
        }
        self._tasks: list[asyncio.Task] = []

    # ---- lifecycle ----

    async def start(self) -> None:
        self._tasks = [asyncio.create_task(self._sensor_loop(p)) for p in self._providers]
        if self._metrics is not None:
            self._tasks.append(asyncio.create_task(self._persist_loop()))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    def readings(self) -> list[HealthReading]:
        """The newest reading of every sensor, in the order the providers were given."""
        return [self._readings[p.name] for p in self._providers]

    # ---- reading ----

    async def _sensor_loop(self, provider: SensorProvider) -> None:
        while True:
            started = self._clock.now_ms()
            await self._read_once(provider)
            spent_s = elapsed_ms(self._clock.now_ms(), started) / 1000
            await self._sleep(max(0.0, self._interval_s - spent_s))

    async def _read_once(self, provider: SensorProvider) -> None:
        stuck = self._inflight.get(provider.name)
        if stuck is not None and not stuck.done():
            self._publish(provider, None)  # still stuck from an earlier read: do not pile up
            return
        future = self._spawn(provider.read)
        self._inflight[provider.name] = future
        waiting = asyncio.wrap_future(future)
        waiting.add_done_callback(lambda f: f.cancelled() or f.exception())  # mark as looked at
        timer = asyncio.ensure_future(self._sleep(self._timeout_s))
        try:
            await asyncio.wait({waiting, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            timer.cancel()
        value = None
        if waiting.done() and not waiting.cancelled() and waiting.exception() is None:
            value = _as_number(waiting.result())
        self._publish(provider, value)

    def _publish(self, provider: SensorProvider, value: float | None) -> None:
        state = AVAILABLE if value is not None else UNAVAILABLE
        self._readings[provider.name] = HealthReading(
            provider.name, value, provider.unit, state, self._clock_us()
        )

    # ---- saving gauges ----

    async def _persist_loop(self) -> None:
        while True:
            await self._sleep(self._persist_s)
            for reading in self.readings():
                if reading.value is not None:
                    self._metrics.gauge(f"laptop_{reading.name}", reading.value)
