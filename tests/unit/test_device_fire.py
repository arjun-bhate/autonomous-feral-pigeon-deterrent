"""Tests for FIRE_REQUEST handling (T025a), shot end and cooldown (T025b)."""

import pytest

from deterrent.protocol.codes import (
    DenyReason,
    EffectorEvent,
    EventType,
    MsgType,
    RejectCode,
    Result,
    State,
)
from deterrent.protocol.messages import (
    Arm,
    Disarm,
    EffectorEventMessage,
    FireRequest,
    Heartbeat,
    Move,
    Reply,
    ZoneBegin,
    host_frame,
    unpack_fault_mask,
    unpack_granted_on_ms,
)
from deterrent.simulator.zone_storage import StoredZone
from support.device_harness import Harness


def ready(**overrides) -> Harness:
    """Armed, key on, heartbeats alive, person clear for longer than the re-engage delay.

    A granted device stays in DETER with the effector on until T025b adds the shot end,
    so every test starts from a fresh one.
    """
    harness = Harness.homed(key_on=True, **overrides)
    for _ in range(27):  # 5.4 s of clear heartbeats every 200 ms
        harness.heartbeat()
        harness.advance(200)
    harness.heartbeat()
    assert harness.reply(Arm()).result is Result.OK
    return harness


def request(harness: Harness, duration_ms: int = 1500, ttl_ms: int = 500, age_ms: int = 0):
    now = harness.clock.now_ms()
    return FireRequest((now - age_ms) % 2**32, ttl_ms, 12000, 4000, duration_ms)


def effector_events(harness: Harness):
    return [
        o.message for o in harness.events_of(EventType.EFFECTOR_EVENT)
    ]  # decoded EffectorEventMessage objects


def denied(reason: DenyReason, fault_mask: int = 0) -> Reply:
    return Reply.denied(reason, fault_mask)


# ---- a grant --------------------------------------------------------------------------------


def test_all_conditions_true_is_granted_and_the_effector_is_on():
    harness = ready()
    reply = harness.reply(request(harness))
    assert reply.result is Result.GRANTED
    assert reply.code == 0
    assert harness.device.effector_on is True
    assert harness.device.state is State.DETER
    assert harness.device.armed is True


def test_the_granted_reply_carries_the_grant_time():
    harness = ready()
    harness.clock.advance(37)
    reply = harness.reply(request(harness))
    assert unpack_granted_on_ms(reply.data) == harness.clock.now_ms()


def test_one_effector_on_event_per_grant_and_none_for_a_denial():
    harness = ready()
    harness.reply(Disarm())
    harness.reply(request(harness))  # denied: not armed
    assert effector_events(harness) == []
    harness.reply(Arm())
    harness.reply(request(harness))
    assert effector_events(harness) == [EffectorEventMessage(EffectorEvent.ON, 0)]
    assert len(harness.sink.named("effector_on")) == 1
    assert len(harness.sink.named("fire_granted")) == 1


def test_the_status_shows_the_effector_on_after_a_grant():
    harness = ready()
    harness.reply(request(harness))
    status = harness.events_of(EventType.STATUS)[-1].message.status
    assert (status.state, status.effector_on) == (State.DETER, True)


# ---- representative denials: permanent code, effector off, state unchanged --------------------


def assert_denied_cleanly(harness: Harness, reply, reason: DenyReason, state: State) -> None:
    assert reply == denied(reason)
    assert harness.device.effector_on is False
    assert harness.device.state is state
    assert harness.sink.named("effector_on") == []
    assert harness.sink.named("fire_denied")[-1].details["reason"] is reason


def test_denied_first_rung_heartbeat_lost():
    harness = Harness.homed(key_on=True)
    harness.reply(Arm())  # no heartbeat has ever been accepted
    reply = harness.reply(request(harness))
    assert_denied_cleanly(harness, reply, DenyReason.HEARTBEAT_LOST, State.SCANNING)
    assert reply.code == 1


def test_denied_middle_rung_not_armed():
    harness = ready()
    harness.reply(Disarm())
    reply = harness.reply(request(harness))
    assert_denied_cleanly(harness, reply, DenyReason.NOT_ARMED, State.DISARMED)
    assert reply.code == 6


def test_person_clear_delay_is_code_14_not_its_priority_8():
    harness = Harness.homed(key_on=True)
    for _ in range(10):  # only 2 s of clear heartbeats
        harness.heartbeat()
        harness.advance(200)
    harness.heartbeat()
    harness.reply(Arm())
    reply = harness.reply(request(harness))
    assert_denied_cleanly(harness, reply, DenyReason.PERSON_CLEAR_DELAY, State.SCANNING)
    assert reply.code == 14


def test_elevation_limit_is_code_10_not_its_priority_11():
    harness = ready(elevation_max_cdeg=3000)
    harness.reply(Move(6000, 4000))
    reply = harness.reply(request(harness))
    assert_denied_cleanly(harness, reply, DenyReason.ELEVATION_LIMIT, State.SCANNING)
    assert reply.code == 10


def test_a_person_report_denies_with_person_not_clear():
    harness = ready()
    harness.reply(Heartbeat(False, 0))
    reply = harness.reply(request(harness))
    assert_denied_cleanly(harness, reply, DenyReason.PERSON_NOT_CLEAR, State.SCANNING)


def test_fault_active_carries_the_fault_mask():
    harness = ready()
    harness.device.fault_mask = 0x0004  # injected reserved bit
    reply = harness.reply(request(harness))
    assert reply.result is Result.DENIED
    assert reply.code == int(DenyReason.FAULT_ACTIVE)
    assert unpack_fault_mask(reply.data) == 0x0004


def test_other_denials_carry_a_zero_fault_mask():
    harness = ready()
    harness.reply(Disarm())
    assert unpack_fault_mask(harness.reply(request(harness)).data) == 0


def test_an_expired_request_is_denied():
    harness = ready()
    reply = harness.reply(request(harness, age_ms=501))
    assert_denied_cleanly(harness, reply, DenyReason.REQUEST_EXPIRED, State.SCANNING)
    assert reply.code == 8


def test_a_corrupt_zone_store_denies_the_request():
    harness = ready()
    harness.storage.write_zone(StoredZone(1, 0, 3, bytes(12)))  # CRC does not match
    reply = harness.reply(request(harness))
    assert_denied_cleanly(harness, reply, DenyReason.ZONE_STORE_INVALID, State.SCANNING)


# ---- duration ---------------------------------------------------------------------------------


@pytest.mark.parametrize("duration", [500, 5000])
def test_duration_500_and_5000_are_granted(duration):
    harness = ready()
    assert harness.reply(request(harness, duration_ms=duration)).result is Result.GRANTED


@pytest.mark.parametrize("duration", [0, 499])
def test_duration_below_500_is_field_range_and_never_reaches_arbitration(duration):
    harness = ready()
    reply = harness.reply(request(harness, duration_ms=duration))
    assert reply == Reply.rejected(MsgType.FIRE_REQUEST, RejectCode.FIELD_RANGE)
    assert harness.device.effector_on is False
    assert harness.sink.named("fire_denied") == []


def test_duration_5001_is_denied_on_time_limit():
    harness = ready()
    reply = harness.reply(request(harness, duration_ms=5001))
    assert_denied_cleanly(harness, reply, DenyReason.ON_TIME_LIMIT, State.SCANNING)
    assert reply.code == 12


def test_the_per_shot_limit_follows_the_setting():
    harness = ready(per_shot_ms=1000)
    assert harness.reply(request(harness, duration_ms=1001)).code == int(DenyReason.ON_TIME_LIMIT)


def test_a_bad_duration_is_field_range_in_every_state_even_before_homing():
    for harness in (Harness(boot_duration_ms=500), Harness.homed(), ready()):
        reply = harness.reply(request(harness, duration_ms=0))
        assert reply == Reply.rejected(MsgType.FIRE_REQUEST, RejectCode.FIELD_RANGE)


# ---- duplicates and a shot already running ------------------------------------------------------


def test_a_duplicate_request_gets_the_cached_reply_and_only_one_grant():
    harness = ready()
    wire = host_frame(request(harness), 4000)
    (first,) = harness.send_wire(wire)
    (second,) = harness.send_wire(wire)
    assert second.raw == first.raw
    assert len(harness.sink.named("effector_on")) == 1
    assert len(effector_events(harness)) == 1


def test_a_second_request_while_a_shot_is_running_is_refused_and_never_extends_it():
    harness = ready()
    harness.reply(request(harness))
    reply = harness.reply(request(harness, duration_ms=5000))
    assert reply == Reply.rejected(MsgType.FIRE_REQUEST, RejectCode.WRONG_STATE)
    assert len(harness.sink.named("effector_on")) == 1


# ---- every other state --------------------------------------------------------------------------


def test_fire_in_boot_is_rejected_not_homed_without_arbitration():
    harness = Harness(boot_duration_ms=500)
    reply = harness.reply(request(harness))
    assert reply == Reply.rejected(MsgType.FIRE_REQUEST, RejectCode.NOT_HOMED)
    assert reply.result is Result.REJECTED
    assert harness.sink.named("fire_denied") == []
    assert harness.device.effector_on is False


def test_fire_in_homing_goes_through_arbitration_and_is_denied_not_homed():
    harness = Harness()
    harness.advance(100)
    harness.heartbeat()  # alive, so rung 1 passes and rung 4 is the first failure
    assert harness.device.state is State.HOMING
    reply = harness.reply(request(harness))
    assert reply == denied(DenyReason.NOT_HOMED)
    assert reply.result is Result.DENIED
    assert [e.details["reason"] for e in harness.sink.named("fire_denied")] == [
        DenyReason.NOT_HOMED
    ]


def test_fire_in_disarmed_is_denied_not_armed():
    harness = Harness.homed(key_on=True)
    harness.heartbeat()
    assert harness.reply(request(harness)) == denied(DenyReason.NOT_ARMED)


def test_fire_in_zone_setup_is_denied_zone_setup():
    harness = Harness.homed(key_on=True)
    harness.heartbeat()
    harness.reply(ZoneBegin(1, 3))
    assert harness.device.state is State.ZONE_SETUP
    assert harness.reply(request(harness)) == denied(DenyReason.ZONE_SETUP)
    assert harness.device.state is State.ZONE_SETUP


def test_fire_in_safe_error_is_denied_with_the_failing_condition():
    harness = Harness.homed(key_on=True)
    harness.heartbeat()
    harness.control.set_key(False)
    assert harness.device.state is State.SAFE_ERROR
    assert harness.reply(request(harness)) == denied(DenyReason.KEY_OFF)
    lost = Harness.homed(key_on=True)
    lost.heartbeat()
    lost.advance(1001)
    assert lost.device.state is State.SAFE_ERROR
    assert lost.reply(request(lost)) == denied(DenyReason.HEARTBEAT_LOST)
