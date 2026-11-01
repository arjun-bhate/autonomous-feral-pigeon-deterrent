"""Clocks for the deterrent system.

Every timing rule in the protocol is measured on one millisecond clock that wraps
around to 0 after 2**32 ms (about 49 days), the same as the ESP32's ``device_ms``.
Code that needs the time asks a ``Clock`` object instead of calling the operating
system, so tests can use a ``FakeClock`` and move time by hand.
"""

import time
from typing import Protocol, runtime_checkable

U32_MODULUS = 2**32
_U32_MASK = U32_MODULUS - 1


@runtime_checkable
class Clock(Protocol):
    """Anything that can report the current time in milliseconds (0 to 2**32 - 1)."""

    def now_ms(self) -> int: ...


class SystemClock:
    """The real clock: milliseconds from the operating system's monotonic timer.

    "Monotonic" means it never goes backwards, even if someone changes the date.
    """

    def now_ms(self) -> int:
        return (time.monotonic_ns() // 1_000_000) & _U32_MASK


class FakeClock:
    """A clock for tests. Time stands still until ``advance`` is called."""

    def __init__(self, start_ms: int = 0) -> None:
        self._now = start_ms & _U32_MASK

    def now_ms(self) -> int:
        return self._now

    def advance(self, ms: int) -> None:
        if ms < 0:
            raise ValueError("a clock cannot go backwards")
        self._now = (self._now + ms) & _U32_MASK


def elapsed_ms(now: int, earlier: int) -> int:
    """Milliseconds from ``earlier`` to ``now``, correct even if the clock wrapped.

    Subtracting modulo 2**32 gives the right answer as long as the real gap is
    less than about 24 days, which is far longer than any timer in the protocol.
    """
    return (now - earlier) & _U32_MASK
