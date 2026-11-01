"""The one place a fire request is decided.

``decide_fire`` is a *pure function*: it only reads the snapshot and request it is
given, never the clock, never a file, and changes nothing. It checks the fourteen
conditions of protocol.md section 6 in priority order and returns the first one that
fails, or ``None`` meaning "granted". There is no other grant path anywhere in the
simulator: if this returns a reason, the effector stays off.

``lapsed_interlock`` uses the same ladder while a shot is running. It re-checks the
conditions that can lapse during a shot and skips the three that cannot: the age of
the request, the requested duration (both are properties of the request, checked once
at admission) and the cooldown (it only starts when a shot ends).
"""

from dataclasses import dataclass

from deterrent.clock import elapsed_ms
from deterrent.geometry.polygon import Point, in_zone_margin
from deterrent.protocol.codes import (
    DENIAL_LADDER,
    DenyReason,
    Fault,
    ReasonStatus,
    State,
)
from deterrent.protocol.messages import FireRequest
from deterrent.simulator.person import PersonCheck


@dataclass(frozen=True)
class FireSnapshot:
    """A read-only picture of the device at the moment of the decision."""

    now_ms: int
    state: State
    homed: bool
    armed: bool
    key_on: bool  # the live key level
    fault_mask: int
    last_heartbeat_ms: int | None  # receive time of the last accepted heartbeat; None if none
    watchdog_ms: int
    person: PersonCheck
    zone_store_valid: bool
    pan: int  # the device's own measured angles
    tilt: int
    elevation_min_cdeg: int
    elevation_max_cdeg: int
    zones: tuple[tuple[Point, ...], ...]  # every stored zone as a polygon
    zone_margin_cdeg: int
    per_shot_ms: int
    cooldown_started_ms: int | None
    cooldown_ms: int


# Each rule answers: does this condition FAIL? (True means the request must be denied.)
def _heartbeat_lost(s: FireSnapshot, r: FireRequest | None) -> bool:
    return s.last_heartbeat_ms is None or elapsed_ms(s.now_ms, s.last_heartbeat_ms) > s.watchdog_ms


def _fault_active(s: FireSnapshot, r: FireRequest | None) -> bool:
    return bool(s.fault_mask & ~int(Fault.HEARTBEAT_LOST | Fault.KEY_OFF))


def _zone_setup(s: FireSnapshot, r: FireRequest | None) -> bool:
    return s.state is State.ZONE_SETUP


def _not_homed(s: FireSnapshot, r: FireRequest | None) -> bool:
    return not s.homed


def _key_off(s: FireSnapshot, r: FireRequest | None) -> bool:
    return not s.key_on


def _not_armed(s: FireSnapshot, r: FireRequest | None) -> bool:
    return not s.armed


def _person_not_clear(s: FireSnapshot, r: FireRequest | None) -> bool:
    return s.person.present


def _person_clear_delay(s: FireSnapshot, r: FireRequest | None) -> bool:
    return not s.person.delay_elapsed


def _request_expired(s: FireSnapshot, r: FireRequest | None) -> bool:
    # Unsigned wrap-safe age: a request dated in the future looks very old and is refused.
    return r is not None and elapsed_ms(s.now_ms, r.issued_at_ms) > r.ttl_ms


def _zone_store_invalid(s: FireSnapshot, r: FireRequest | None) -> bool:
    return not s.zone_store_valid


def _elevation_limit(s: FireSnapshot, r: FireRequest | None) -> bool:
    return not s.elevation_min_cdeg <= s.tilt <= s.elevation_max_cdeg


def _in_zone_margin(s: FireSnapshot, r: FireRequest | None) -> bool:
    head = (s.pan, s.tilt)
    return any(in_zone_margin(head, polygon, s.zone_margin_cdeg) for polygon in s.zones)


def _on_time_limit(s: FireSnapshot, r: FireRequest | None) -> bool:
    return r is not None and r.duration_ms > s.per_shot_ms


def _cooldown_active(s: FireSnapshot, r: FireRequest | None) -> bool:
    return (
        s.cooldown_started_ms is not None
        and elapsed_ms(s.now_ms, s.cooldown_started_ms) < s.cooldown_ms
    )


_RULES = {
    DenyReason.HEARTBEAT_LOST: _heartbeat_lost,
    DenyReason.FAULT_ACTIVE: _fault_active,
    DenyReason.ZONE_SETUP: _zone_setup,
    DenyReason.NOT_HOMED: _not_homed,
    DenyReason.KEY_OFF: _key_off,
    DenyReason.NOT_ARMED: _not_armed,
    DenyReason.PERSON_NOT_CLEAR: _person_not_clear,
    DenyReason.PERSON_CLEAR_DELAY: _person_clear_delay,
    DenyReason.REQUEST_EXPIRED: _request_expired,
    DenyReason.ZONE_STORE_INVALID: _zone_store_invalid,
    DenyReason.ELEVATION_LIMIT: _elevation_limit,
    DenyReason.IN_ZONE_MARGIN: _in_zone_margin,
    DenyReason.ON_TIME_LIMIT: _on_time_limit,
    DenyReason.COOLDOWN_ACTIVE: _cooldown_active,
}

# The ladder in priority order, taken from the denial table in codes.py.
_LADDER: tuple[DenyReason, ...] = tuple(
    DenyReason[entry.name]
    for entry in sorted(
        (e for e in DENIAL_LADDER if e.status is ReasonStatus.ACTIVE),
        key=lambda e: e.priority,
    )
)
assert set(_LADDER) == set(_RULES), "every active denial reason needs exactly one rule"

# Conditions that cannot lapse while a shot is running.
_NOT_RECHECKED = frozenset(
    {DenyReason.REQUEST_EXPIRED, DenyReason.ON_TIME_LIMIT, DenyReason.COOLDOWN_ACTIVE}
)


def decide_fire(snapshot: FireSnapshot, request: FireRequest) -> DenyReason | None:
    """Return the first failing condition, or None if the request may be granted."""
    for reason in _LADDER:
        if _RULES[reason](snapshot, request):
            return reason
    return None


def lapsed_interlock(snapshot: FireSnapshot) -> DenyReason | None:
    """While the effector is on: the first safety condition that no longer holds, if any."""
    for reason in _LADDER:
        if reason not in _NOT_RECHECKED and _RULES[reason](snapshot, None):
            return reason
    return None
