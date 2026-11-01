"""Runs the simulated device inside an asyncio program.

``asyncio`` is Python's built-in way to do several things "at once" in one thread: a
*task* is a piece of code that runs until it has to wait (for bytes to arrive, for
time to pass), and while it waits other tasks run.

The runner owns two tasks and nothing else:

* a **reader** that waits for bytes from the link, hands them to the device and sends
  back whatever the device answers;
* a **ticker** that calls the device's ``tick`` at most every 10 ms and sends back
  whatever the device reports on its own.

There is deliberately **no decision logic here**. The runner knows only two device
methods, ``handle_bytes`` and ``tick``; it cannot see or change the device state, the
arbitration inputs or the effector. A test enforces both facts.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

MAX_TICK_INTERVAL_MS = 10


class DeviceLink(Protocol):
    """Where bytes come from and go to (a socket, a serial port, or a test queue)."""

    async def receive(self) -> bytes | None:
        """Wait for the next chunk of bytes. Returns None when the link has closed."""
        ...

    async def send(self, data: bytes) -> None: ...


class RunnableDevice(Protocol):
    """The only two things the runner is allowed to ask of the device."""

    def handle_bytes(self, data: bytes) -> list[bytes]: ...

    def tick(self) -> list[bytes]: ...


class SimulatorRunner:
    def __init__(
        self,
        device: RunnableDevice,
        link: DeviceLink,
        *,
        tick_interval_ms: int = MAX_TICK_INTERVAL_MS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not 1 <= tick_interval_ms <= MAX_TICK_INTERVAL_MS:
            raise ValueError(f"tick interval must be 1 to {MAX_TICK_INTERVAL_MS} ms")
        self._device = device
        self._link = link
        self._tick_interval_s = tick_interval_ms / 1000
        self._sleep = sleep
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._read_loop()),
            asyncio.create_task(self._tick_loop()),
        ]

    async def stop(self) -> None:
        """Stop both tasks. If either failed with an error, that error is raised here."""
        for task in self._tasks:
            task.cancel()
        results = await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                raise result

    async def _publish(self, frames: list[bytes]) -> None:
        for frame in frames:
            await self._link.send(frame)

    async def _read_loop(self) -> None:
        while True:
            data = await self._link.receive()
            if data is None:
                return  # the link closed; ticking carries on until stop()
            await self._publish(self._device.handle_bytes(data))

    async def _tick_loop(self) -> None:
        while True:
            await self._publish(self._device.tick())
            await self._sleep(self._tick_interval_s)
