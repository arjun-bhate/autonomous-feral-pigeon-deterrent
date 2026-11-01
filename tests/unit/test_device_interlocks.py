"""Tests for continuous interlock checking while the effector is on (T026)."""

import pytest

from deterrent.protocol.codes import DenyReason, EffectorEvent, EventType, Fault, Result, State
from deterrent.protocol.messages import (
    Arm,
    Disarm,
    EffectorEventMessage,
    FireRequest,
    Heartbeat,
    Move,
)
from support.device_harness import Harness

ZONE = [(6000, 2000), (7000, 2000), (7000, 3000), (6000, 3000)]  # far from the head at (0, 0)
WITHIN_100_MS = 100


def ready_to_fire(**overrides) -> Harness:
    """Armed, host alive and clear for over 5 s, one zone stored. The head is at (0, 0)."""
    harness = Harness.homed(key_on=True, **overrides)
    harness.store_zone(1, ZONE)
    for _ in range(27):
        harness.heartbeat()
        harness.advance(200)
    harness.heartbeat()
    assert harness.reply(Arm()).result is Result.OK
    return harness


def fire(harness: Harness, duration_ms: int = 5000, ttl_ms: int = 500) -> int:
    now = harness.clock.now_ms()
    reply = harness.reply(FireRequest(now, ttl_ms, 0, 0, duration_ms))
    assert reply.result is Result.GRANTED, reply
    return now


def run(harness: Harness, ms: int) -> None:
    remaining = ms
    while remaining > 0:
        step = min(200, remaining)
        harness.advance(step)
        harness.heartbeat()
        remaining -= step


def cuts(harness: Harness):
    return [
        o.message
        for o in harness.events_of(EventType.EFFECTOR_EVENT)
        if o.message.event is EffectorEvent.CUT
    ]


# Each lapse: (how to cause it, cause, resulting state, armed, fault mask, cooldown started).


def lapse_heartbeat_lost(harness):
    start = harness.clock.now_ms()
    harness.advance(1001)  # no heartbeat for more than watchdog_ms
    return start + 1001


def lapse_person(harness):
    harness.reply(Heartbeat(False, 0))
    return harness.clock.now_ms()


def lapse_key_off(harness):
    harness.control.set_key(False)
    harness.tick()
    return harness.clock.now_ms()


def lapse_zone_margin(harness):
    harness.reply(Move(6500, 2500))
    return harness.clock.now_ms()


def lapse_elevation(harness):
    harness.reply(Move(0, 4000))  # the limit is lowered to 3000 in this case
    return harness.clock.now_ms()


def lapse_store_corrupted(harness):
    harness.storage.corrupt_zone(1)
    moment = harness.clock.now_ms()
    harness.advance(10)
    return moment


def lapse_disarm(harness):
    harness.reply(Disarm())
    return harness.clock.now_ms()


def lapse_fault(harness):
    harness.device.fault_mask |= int(Fault.HOMING_FAILED)  # a reserved bit, injected
    moment = harness.clock.now_ms()
    harness.advance(10)
    return moment


CASES = [
    pytest.param(
        lapse_heartbeat_lost,
        {},
        DenyReason.HEARTBEAT_LOST,
        State.SAFE_ERROR,
        False,
        int(Fault.HEARTBEAT_LOST),
        False,
        id="heartbeat_lost",
    ),
    pytest.param(
        lapse_person, {}, DenyReason.PERSON_NOT_CLEAR, State.COOLDOWN, True, 0, True, id="person"
    ),
    pytest.param(
        lapse_key_off,
        {},
        DenyReason.KEY_OFF,
        State.SAFE_ERROR,
        False,
        int(Fault.KEY_OFF),
        False,
        id="key_off",
    ),
    pytest.param(
        lapse_zone_margin,
        {},
        DenyReason.IN_ZONE_MARGIN,
        State.COOLDOWN,
        True,
        0,
        True,
        id="zone_margin",
    ),
    pytest.param(
        lapse_elevation,
        {"elevation_max_cdeg": 3000},
        DenyReason.ELEVATION_LIMIT,
        State.COOLDOWN,
        True,
        0,
        True,
        id="elevation",
    ),
    pytest.param(
        lapse_store_corrupted,
        {},
        DenyReason.ZONE_STORE_INVALID,
        State.COOLDOWN,
        True,
        0,
        True,
        id="store_invalid",
    ),
    pytest.param(
        lapse_disarm, {}, DenyReason.NOT_ARMED, State.DISARMED, False, 0, True, id="disarm"
    ),
    pytest.param(
        lapse_fault,
        {},
        DenyReason.FAULT_ACTIVE,
        State.COOLDOWN,
        True,
        int(Fault.HOMING_FAILED),
        True,
        id="fault_active",
    ),
]


@pytest.mark.parametrize(
    ("lapse", "overrides", "cause", "state", "armed", "fault_mask", "cooldown"), CASES
)
def test_each_lapse_cuts_the_effector_and_has_the_documented_outcome(
    lapse, overrides, cause, state, armed, fault_mask, cooldown
):
    harness = ready_to_fire(**overrides)
    fire(harness)
    moment = lapse(harness)
    harness.advance(10)

    assert harness.device.effector_on is False
    cut_times = [e.device_ms for e in harness.sink.named("effector_cut")]
    assert len(cut_times) == 1
    assert 0 <= cut_times[0] - moment <= WITHIN_100_MS
    assert cuts(harness) == [EffectorEventMessage(EffectorEvent.CUT, int(cause))]
    assert harness.device.state is state
    assert harness.device.armed is armed
    assert harness.device.fault_mask == fault_mask
    assert harness.device.cooldown_running() is cooldown
    assert [e.details["cause"] for e in harness.sink.named("interlock_event")] == [cause]
    assert harness.sink.named("effector_ended") == []


def test_every_cause_code_is_the_permanent_code_not_the_priority():
    assert int(DenyReason.IN_ZONE_MARGIN) == 11
    assert int(DenyReason.ELEVATION_LIMIT) == 10
    assert int(DenyReason.ZONE_STORE_INVALID) == 9
    harness = ready_to_fire()
    fire(harness)
    lapse_zone_margin(harness)
    assert cuts(harness)[0].cause == 11


def test_ttl_expiry_in_the_middle_of_a_shot_causes_no_cut():
    harness = ready_to_fire()
    fire(harness, duration_ms=5000, ttl_ms=100)
    run(harness, 3000)  # the 100 ms TTL passed long ago
    assert harness.device.effector_on is True
    assert cuts(harness) == []


def test_a_cut_shot_is_never_resumed_after_the_delay_elapses():
    harness = ready_to_fire()
    fire(harness)
    lapse_person(harness)
    ons_before = len(harness.sink.named("effector_on"))
    run(harness, 12_000)  # the 5 s delay and the 5 s cooldown are both over
    assert harness.device.effector_on is False
    assert len(harness.sink.named("effector_on")) == ons_before
    assert harness.device.state is State.SCANNING


def test_a_new_request_after_a_person_cut_and_the_cooldown_is_granted():
    harness = ready_to_fire()
    fire(harness)
    lapse_person(harness)
    run(harness, 5200)  # person clear again for over 5 s and the cooldown has ended
    assert fire(harness, duration_ms=500) is not None
    assert harness.device.effector_on is True


def test_a_request_during_the_cooldown_after_a_cut_is_denied():
    harness = ready_to_fire()
    fire(harness)
    lapse_person(harness)
    run(harness, 1000)
    now = harness.clock.now_ms()
    reply = harness.reply(FireRequest(now, 500, 0, 0, 500))
    assert reply.result is Result.DENIED


def test_one_interlock_event_per_lapse_even_over_many_ticks():
    harness = ready_to_fire()
    fire(harness)
    harness.device.fault_mask |= int(Fault.HOMING_FAILED)
    for _ in range(50):
        harness.advance(10)
    assert len(harness.sink.named("interlock_event")) == 1
    assert len(cuts(harness)) == 1


def test_the_check_runs_on_every_tick_not_only_on_messages():
    harness = ready_to_fire()
    fire(harness)
    run(harness, 200)
    harness.storage.corrupt_zone(1)
    before = harness.clock.now_ms()
    harness.advance(10)  # one tick, no message at all
    assert harness.device.effector_on is False
    assert harness.clock.now_ms() - before <= 10


def test_a_cut_does_not_latch_a_fault_for_conditions_other_than_heartbeat_and_key():
    for lapse, overrides in [(lapse_person, {}), (lapse_zone_margin, {})]:
        harness = ready_to_fire(**overrides)
        fire(harness)
        lapse(harness)
        assert harness.device.fault_mask == 0
        assert harness.device.armed is True


def test_the_effector_cannot_be_on_in_a_state_where_a_lapse_ended_the_shot():
    harness = ready_to_fire()
    fire(harness)
    lapse_zone_margin(harness)
    assert harness.device.effector_on is False
    status = harness.events_of(EventType.STATUS)[-1].message.status
    assert status.effector_on is False
