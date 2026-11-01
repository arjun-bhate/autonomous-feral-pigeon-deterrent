"""Tests for the fire arbitration function (T024). This is the only grant path."""

import ast
from dataclasses import replace
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from deterrent.protocol.codes import DENIAL_LADDER, DenyReason, ReasonStatus, State
from deterrent.protocol.messages import FireRequest
from deterrent.simulator import arbitration
from deterrent.simulator.arbitration import FireSnapshot, decide_fire, lapsed_interlock
from deterrent.simulator.person import PersonCheck

ZONE = ((0, 0), (1000, 0), (1000, 1000), (0, 1000))
NOW = 100_000

BASELINE = FireSnapshot(
    now_ms=NOW,
    state=State.SCANNING,
    homed=True,
    armed=True,
    key_on=True,
    fault_mask=0,
    last_heartbeat_ms=NOW - 100,
    watchdog_ms=1000,
    person=PersonCheck(present=False, delay_elapsed=True),
    zone_store_valid=True,
    pan=12000,
    tilt=4000,
    elevation_min_cdeg=0,
    elevation_max_cdeg=8000,
    zones=(ZONE,),
    zone_margin_cdeg=70,
    per_shot_ms=5000,
    cooldown_started_ms=None,
    cooldown_ms=5000,
)
REQUEST = FireRequest(
    issued_at_ms=NOW - 100, ttl_ms=500, intended_pan=12000, intended_tilt=4000, duration_ms=1500
)

# Each entry changes exactly ONE condition of the baseline so that only it fails.
# A mutation takes (snapshot, request) and returns the changed pair.
AROUND_HEAD = ((11000, 3000), (13000, 3000), (13000, 5000), (11000, 5000))


def snap(**changes):
    return lambda s, r: (replace(s, **changes), r)


def req(**changes):
    return lambda s, r: (s, replace(r, **changes))


MUTATIONS = {
    DenyReason.HEARTBEAT_LOST: snap(last_heartbeat_ms=None),
    DenyReason.FAULT_ACTIVE: snap(fault_mask=0x0004),
    DenyReason.ZONE_SETUP: snap(state=State.ZONE_SETUP),
    DenyReason.NOT_HOMED: snap(homed=False),
    DenyReason.KEY_OFF: snap(key_on=False),
    DenyReason.NOT_ARMED: snap(armed=False),
    # These two change one field each, so applying both keeps both failures.
    DenyReason.PERSON_NOT_CLEAR: lambda s, r: (
        replace(s, person=replace(s.person, present=True)),
        r,
    ),
    DenyReason.PERSON_CLEAR_DELAY: lambda s, r: (
        replace(s, person=replace(s.person, delay_elapsed=False)),
        r,
    ),
    DenyReason.REQUEST_EXPIRED: req(issued_at_ms=NOW - 501),
    DenyReason.ZONE_STORE_INVALID: snap(zone_store_valid=False),
    DenyReason.ELEVATION_LIMIT: snap(elevation_max_cdeg=3000),  # head at tilt 4000 is now too high
    DenyReason.IN_ZONE_MARGIN: snap(zones=(AROUND_HEAD,)),
    DenyReason.ON_TIME_LIMIT: req(duration_ms=5001),
    DenyReason.COOLDOWN_ACTIVE: snap(cooldown_started_ms=NOW - 100),
}
ACTIVE = [entry for entry in DENIAL_LADDER if entry.status is ReasonStatus.ACTIVE]
PRIORITY = {DenyReason[entry.name]: entry.priority for entry in ACTIVE}


def apply(*reasons):
    snapshot, request = BASELINE, REQUEST
    for reason in reasons:
        snapshot, request = MUTATIONS[reason](snapshot, request)
    return snapshot, request


# ---- the baseline and one test per reason -------------------------------------------------------


def test_the_baseline_where_everything_passes_is_granted():
    assert decide_fire(BASELINE, REQUEST) is None


def test_every_active_reason_has_a_mutation():
    assert set(MUTATIONS) == {DenyReason[entry.name] for entry in ACTIVE}
    assert len(MUTATIONS) == 14


@pytest.mark.parametrize("reason", list(MUTATIONS), ids=lambda r: r.name)
def test_only_that_condition_fails_and_the_reply_names_it(reason):
    snapshot, request = apply(reason)
    assert decide_fire(snapshot, request) is reason


def test_a_stale_heartbeat_is_heartbeat_lost_too():
    old = replace(BASELINE, last_heartbeat_ms=NOW - 1001)
    assert decide_fire(old, REQUEST) is DenyReason.HEARTBEAT_LOST
    exactly = replace(BASELINE, last_heartbeat_ms=NOW - 1000)
    assert decide_fire(exactly, REQUEST) is None


def test_fault_active_ignores_heartbeat_lost_and_key_off_bits():
    for mask in (0x0001, 0x0010, 0x0011):
        assert decide_fire(replace(BASELINE, fault_mask=mask), REQUEST) is None
    for mask in (0x0002, 0x0004, 0x0008, 0x0020, 0x8000, 0x0015):
        assert decide_fire(replace(BASELINE, fault_mask=mask), REQUEST) is DenyReason.FAULT_ACTIVE


@pytest.mark.parametrize("tilt", [-1, 8001])
def test_tilt_outside_the_elevation_limits_is_denied(tilt):
    assert decide_fire(replace(BASELINE, tilt=tilt), REQUEST) is DenyReason.ELEVATION_LIMIT


@pytest.mark.parametrize("tilt", [0, 8000])
def test_tilt_exactly_at_a_limit_is_allowed(tilt):
    assert decide_fire(replace(BASELINE, tilt=tilt, zones=()), REQUEST) is None


def test_the_margin_around_a_zone_denies_and_just_outside_it_does_not():
    near = replace(BASELINE, pan=1070, tilt=500)  # exactly 70 from the zone edge
    assert decide_fire(near, REQUEST) is DenyReason.IN_ZONE_MARGIN
    far = replace(BASELINE, pan=1071, tilt=500)
    assert decide_fire(far, REQUEST) is None


def test_every_zone_is_checked():
    second = ((5000, 0), (6000, 0), (6000, 1000), (5000, 1000))
    snapshot = replace(BASELINE, zones=(ZONE, second), pan=5500, tilt=500)
    assert decide_fire(snapshot, REQUEST) is DenyReason.IN_ZONE_MARGIN


def test_a_decision_uses_the_devices_own_angles_not_the_requested_direction():
    far_request = replace(REQUEST, intended_pan=500, intended_tilt=500)  # points into the zone
    assert decide_fire(BASELINE, far_request) is None
    inside = replace(BASELINE, pan=500, tilt=500)
    safe_request = replace(REQUEST, intended_pan=12000, intended_tilt=4000)
    assert decide_fire(inside, safe_request) is DenyReason.IN_ZONE_MARGIN


# ---- boundaries ---------------------------------------------------------------------------------


def test_request_expiry_boundary():
    at_limit = replace(REQUEST, issued_at_ms=NOW - 500)  # now == issued + ttl
    assert decide_fire(BASELINE, at_limit) is None
    one_late = replace(REQUEST, issued_at_ms=NOW - 501)  # now == issued + ttl + 1
    assert decide_fire(BASELINE, one_late) is DenyReason.REQUEST_EXPIRED


def test_a_request_dated_in_the_future_is_treated_as_expired():
    future = replace(REQUEST, issued_at_ms=NOW + 1)
    assert decide_fire(BASELINE, future) is DenyReason.REQUEST_EXPIRED


def test_request_expiry_is_wrap_safe():
    wrapped_now = replace(BASELINE, now_ms=100, last_heartbeat_ms=50)
    issued = (100 - 500) % 2**32
    assert decide_fire(wrapped_now, replace(REQUEST, issued_at_ms=issued)) is None
    assert (
        decide_fire(wrapped_now, replace(REQUEST, issued_at_ms=(issued - 1) % 2**32))
        is DenyReason.REQUEST_EXPIRED
    )


def test_cooldown_boundary():
    one_early = replace(BASELINE, cooldown_started_ms=NOW - 4999)
    assert decide_fire(one_early, REQUEST) is DenyReason.COOLDOWN_ACTIVE
    on_time = replace(BASELINE, cooldown_started_ms=NOW - 5000)
    assert decide_fire(on_time, REQUEST) is None


def test_cooldown_zero_never_denies():
    snapshot = replace(BASELINE, cooldown_started_ms=NOW, cooldown_ms=0)
    assert decide_fire(snapshot, REQUEST) is None


def test_person_delay_boundary_is_the_person_timers_result():
    assert (
        decide_fire(replace(BASELINE, person=PersonCheck(False, False)), REQUEST)
        is DenyReason.PERSON_CLEAR_DELAY
    )
    assert decide_fire(replace(BASELINE, person=PersonCheck(False, True)), REQUEST) is None


def test_on_time_limit_boundary():
    assert decide_fire(BASELINE, replace(REQUEST, duration_ms=5000)) is None
    assert decide_fire(BASELINE, replace(REQUEST, duration_ms=5001)) is DenyReason.ON_TIME_LIMIT
    low = replace(BASELINE, per_shot_ms=1000)
    assert decide_fire(low, replace(REQUEST, duration_ms=1000)) is None
    assert decide_fire(low, replace(REQUEST, duration_ms=1001)) is DenyReason.ON_TIME_LIMIT


# ---- priority order: pairs named by priority number ---------------------------------------------


@pytest.mark.parametrize(
    ("winner", "loser"),
    [
        (7, 8),
        (8, 9),
        (5, 14),
        (8, 14),
        (13, 14),
        (1, 2),
        (3, 4),
        (6, 7),
        (10, 11),
        (11, 12),
        (12, 13),
    ],
)
def test_the_lower_priority_number_is_reported(winner, loser):
    by_number = {number: reason for reason, number in PRIORITY.items()}
    snapshot, request = apply(by_number[winner], by_number[loser])
    assert decide_fire(snapshot, request) is by_number[winner]
    # Applying them in the other order gives the same answer.
    snapshot, request = apply(by_number[loser], by_number[winner])
    assert decide_fire(snapshot, request) is by_number[winner]


def test_everything_failing_at_once_reports_the_first_rung():
    snapshot, request = apply(*MUTATIONS)
    assert decide_fire(snapshot, request) is DenyReason.HEARTBEAT_LOST


@given(st.sets(st.sampled_from(list(MUTATIONS)), min_size=1))
def test_with_any_failing_condition_the_result_is_never_granted_and_is_the_highest_rung(failing):
    snapshot, request = apply(*sorted(failing))
    result = decide_fire(snapshot, request)
    assert result is not None
    assert result is min(failing, key=lambda reason: PRIORITY[reason])


def test_the_retired_reason_is_never_returned():
    for reasons in [list(MUTATIONS), [DenyReason.COOLDOWN_ACTIVE], []]:
        result = decide_fire(*apply(*reasons))
        assert result is not DenyReason.RATE_LIMIT


# ---- the interlocks re-checked while the effector is on -----------------------------------------


INTERLOCKS = [
    reason
    for reason in MUTATIONS
    if reason
    not in (DenyReason.REQUEST_EXPIRED, DenyReason.ON_TIME_LIMIT, DenyReason.COOLDOWN_ACTIVE)
]


def test_nothing_has_lapsed_in_the_baseline():
    assert lapsed_interlock(BASELINE) is None


@pytest.mark.parametrize("reason", INTERLOCKS, ids=lambda r: r.name)
def test_each_interlock_lapse_is_found(reason):
    snapshot, _ = apply(reason)
    assert lapsed_interlock(snapshot) is reason


@pytest.mark.parametrize(
    "reason",
    [DenyReason.REQUEST_EXPIRED, DenyReason.ON_TIME_LIMIT, DenyReason.COOLDOWN_ACTIVE],
    ids=lambda r: r.name,
)
def test_request_properties_and_the_cooldown_are_not_interlocks(reason):
    snapshot, _ = apply(reason)
    assert lapsed_interlock(snapshot) is None


def test_the_first_lapsed_interlock_wins():
    snapshot, _ = apply(DenyReason.KEY_OFF, DenyReason.PERSON_NOT_CLEAR, DenyReason.IN_ZONE_MARGIN)
    assert lapsed_interlock(snapshot) is DenyReason.KEY_OFF


# ---- purity: no I/O, no clock of its own --------------------------------------------------------


def test_the_module_has_no_io_and_no_clock():
    tree = ast.parse(Path(arbitration.__file__).read_text(encoding="utf-8"))
    forbidden_modules = {
        "time",
        "datetime",
        "socket",
        "sqlite3",
        "os",
        "pathlib",
        "asyncio",
        "threading",
        "random",
        "logging",
    }
    forbidden_names = {"open", "print", "input", "SystemClock", "FakeClock", "Clock"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not {a.name.split(".")[0] for a in node.names} & forbidden_modules
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in forbidden_modules
            assert not {a.name for a in node.names} & forbidden_names
        if isinstance(node, ast.Name):
            assert node.id not in forbidden_names, node.id
        if isinstance(node, ast.Attribute):
            assert node.attr not in forbidden_names, node.attr
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr != "now_ms", "the module must not read a clock"


def test_the_decision_does_not_modify_its_inputs():
    snapshot, request = apply(DenyReason.KEY_OFF)
    decide_fire(snapshot, request)
    assert snapshot == replace(BASELINE, key_on=False)
    with pytest.raises(AttributeError):
        snapshot.key_on = True  # frozen: a snapshot is read-only
