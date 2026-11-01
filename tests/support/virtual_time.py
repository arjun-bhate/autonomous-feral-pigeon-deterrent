"""Test helper: time that moves only when the test says so, for code that uses asyncio.sleep.

Several loops run at once in the real program (the device ticker every 10 ms, the
heartbeat every 200 ms, the live hub every second). In a test we want all of them to
see one shared fake clock and to run exactly as if the time had passed, instantly.

``VirtualTime.sleep`` is a stand-in for ``asyncio.sleep``: a task that calls it is parked
until the test moves the clock past its wake-up time. ``advance(ms)`` moves the shared
``FakeClock`` forward to each wake-up time in order, waking the sleeper and letting
every task run until nothing more can happen before moving on.
"""

import asyncio
import heapq
import itertools
import math

from deterrent.clock import FakeClock

SETTLE_ROUNDS = 40


async def settle(rounds: int = SETTLE_ROUNDS) -> None:
    """Let every ready task run (messages passing between tasks take several rounds)."""
    for _ in range(rounds):
        await asyncio.sleep(0)


class VirtualTime:
    def __init__(self, clock: FakeClock | None = None) -> None:
        self.clock = clock if clock is not None else FakeClock()
        self._elapsed_ms = 0
        self._sleepers: list[tuple[int, int, asyncio.Future]] = []
        self._order = itertools.count()

    @property
    def elapsed_ms(self) -> int:
        return self._elapsed_ms

    async def sleep(self, seconds: float) -> None:
        wake = self._elapsed_ms + max(0, math.ceil(seconds * 1000))
        future = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (wake, next(self._order), future))
        await future

    async def advance(self, ms: int) -> None:
        """Move time forward ``ms`` milliseconds, running everything that becomes due."""
        target = self._elapsed_ms + ms
        await settle()
        while self._sleepers and self._sleepers[0][0] <= target:
            wake, _, future = heapq.heappop(self._sleepers)
            self._move_to(wake)
            if not future.done():
                future.set_result(None)
            await settle()
        self._move_to(target)
        await settle()

    def _move_to(self, elapsed_ms: int) -> None:
        self.clock.advance(elapsed_ms - self._elapsed_ms)
        self._elapsed_ms = elapsed_ms
