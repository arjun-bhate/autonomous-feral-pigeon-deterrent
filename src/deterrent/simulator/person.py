"""The two person timers (protocol.md section 5.3), as plain functions of the device clock.

Two different questions are answered here, both using only the time the device
*received* a heartbeat (never the timestamp the laptop wrote inside it):

* **Status hold-off**: how old may the latest person-clear report be? If it is older
  than ``person_clear_holdoff_ms`` it is *stale*, and stale counts as "person
  present" (fail closed: a silent laptop must never look like an empty yard).
* **Re-engage delay**: for how long has "no person" been reported without a break?
  Firing is allowed again only after ``person_reengage_delay_ms`` of that.

``PersonTimers`` holds the three remembered values. ``evaluate_person`` is a pure
function (it only reads its arguments, has no clock and no side effects), so the
fire-arbitration code can call it on a snapshot.
"""

from dataclasses import dataclass

from deterrent.clock import elapsed_ms


@dataclass(frozen=True)
class PersonSnapshot:
    """What the device remembers about person reports."""

    last_rx_ms: int | None  # receive time of the last accepted heartbeat; None before any
    person_clear: bool  # what that heartbeat reported
    clear_since_ms: int | None  # start of the current clear period; None if none exists


@dataclass(frozen=True)
class PersonCheck:
    present: bool  # a person must be assumed present (reported, unknown or stale)
    delay_elapsed: bool  # the clear period is long enough (or the delay is disabled)


def evaluate_person(
    snapshot: PersonSnapshot, now_ms: int, holdoff_ms: int, reengage_delay_ms: int
) -> PersonCheck:
    """Apply both timers to a snapshot at time ``now_ms``."""
    fresh = (
        snapshot.last_rx_ms is not None and elapsed_ms(now_ms, snapshot.last_rx_ms) <= holdoff_ms
    )
    present = not fresh or not snapshot.person_clear
    if present:
        return PersonCheck(present=True, delay_elapsed=False)
    if reengage_delay_ms == 0:
        return PersonCheck(present=False, delay_elapsed=True)  # delay disabled
    elapsed = (
        snapshot.clear_since_ms is not None
        and elapsed_ms(now_ms, snapshot.clear_since_ms) >= reengage_delay_ms
    )
    return PersonCheck(present=False, delay_elapsed=elapsed)


class PersonTimers:
    """Remembers person reports and when the current clear period began."""

    def __init__(self) -> None:
        self._last_rx_ms: int | None = None
        self._person_clear = False
        self._clear_since_ms: int | None = None

    def snapshot(self) -> PersonSnapshot:
        return PersonSnapshot(self._last_rx_ms, self._person_clear, self._clear_since_ms)

    def on_heartbeat(self, now_ms: int, person_clear: bool, holdoff_ms: int) -> None:
        """An accepted heartbeat arrived at ``now_ms`` (call only for accepted ones)."""
        previous_went_stale = (
            self._last_rx_ms is not None and elapsed_ms(now_ms, self._last_rx_ms) > holdoff_ms
        )
        if previous_went_stale or not person_clear:
            self._clear_since_ms = None  # the old clear period ended
        if person_clear and self._clear_since_ms is None:
            self._clear_since_ms = now_ms  # a new one starts at this heartbeat
        self._last_rx_ms = now_ms
        self._person_clear = person_clear

    def restart(self) -> None:
        """Discard the clear period (heartbeat loss, SAFE_ERROR recovery). The next accepted
        clear heartbeat begins a new one; the delay applies again until then."""
        self._clear_since_ms = None
