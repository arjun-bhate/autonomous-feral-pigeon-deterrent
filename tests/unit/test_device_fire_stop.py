"""Tests for FIRE_STOP (T027): accepted in every state, no arbitration, can only switch off."""

from hypothesis import given, settings
from hypothesis import strategies as st

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
    EffectorEventMessage,
    FireRequest,
    FireStop,
    Reply,
    ZoneBegin,
    ZonePoint,
    host_frame,
)
from support.device_harness import Harness

OK = Reply.ok(MsgType.FIRE_STOP)


def ready() -> Harness:
    harness = Harness.homed(key_on=True)
    for _ in range(27):
        harness.heartbeat()
        harness.advance(200)
    harness.heartbeat()
    assert harness.reply(Arm()).result is Result.OK
    return harness


def request(harness: Harness, duration_ms: int = 5000) -> FireRequest:
    return FireRequest(harness.clock.now_ms(), 500, 0, 0, duration_ms)


def run(harness: Harness, ms: int) -> None:
    remaining = ms
    while remaining > 0:
        step = min(200, remaining)
        harness.advance(step)
        harness.heartbeat()
        remaining -= step


def stop_events(harness: Harness):
    return [
        o.message
        for o in harness.events_of(EventType.EFFECTOR_EVENT)
        if o.message.event is EffectorEvent.STOPPED_BY_HOST
    ]


# ---- the protocol table, one test per state -----------------------------------------------------


def test_boot_ok_and_nothing_to_stop():
    harness = Harness(boot_duration_ms=500)
    assert harness.reply(FireStop()) == OK
    assert harness.device.state is State.BOOT


def test_homing_ok_and_homing_continues():
    harness = Harness()
    harness.advance(500)
    assert harness.reply(FireStop()) == OK
    assert harness.device.state is State.HOMING
    harness.advance(2500)
    assert harness.device.state is State.DISARMED
    assert harness.device.homed is True


def test_disarmed_ok_and_nothing_changes():
    harness = Harness.homed()
    before = harness.snapshot()
    assert harness.reply(FireStop()) == OK
    assert harness.snapshot() == before


def test_scanning_ok_and_nothing_changes_no_event_no_cooldown():
    harness = ready()
    before = harness.snapshot()
    events = len(harness.log)
    assert harness.reply(FireStop()) == OK
    assert harness.snapshot() == before
    assert harness.device.cooldown_running() is False
    assert [o for o in harness.log[events:] if o.type == EventType.EFFECTOR_EVENT] == []
    assert harness.sink.named("effector_stopped_by_host") == []


def test_deter_effector_off_event_cooldown_and_state_cooldown():
    harness = ready()
    assert harness.reply(request(harness)).result is Result.GRANTED
    run(harness, 400)
    assert harness.reply(FireStop()) == OK
    assert harness.device.effector_on is False
    assert harness.device.state is State.COOLDOWN
    assert harness.device.armed is True
    assert harness.device.fault_mask == 0
    assert harness.device.cooldown_running() is True
    assert stop_events(harness) == [EffectorEventMessage(EffectorEvent.STOPPED_BY_HOST, 0)]
    # Not an interlock lapse, not a cut.
    assert harness.sink.named("interlock_event") == []
    assert harness.sink.named("effector_cut") == []
    assert len(harness.sink.named("effector_stopped_by_host")) == 1


def test_cooldown_ok_and_the_remaining_time_is_unchanged():
    harness = ready()
    harness.reply(request(harness, 500))
    run(harness, 500)
    assert harness.device.state is State.COOLDOWN
    run(harness, 2000)
    assert harness.reply(FireStop()) == OK
    assert harness.device.state is State.COOLDOWN
    run(harness, 2999)  # 4999 ms after the shot ended
    assert harness.reply(request(harness, 500)) == Reply.denied(DenyReason.COOLDOWN_ACTIVE)
    harness.advance(1)
    assert harness.reply(request(harness, 500)).result is Result.GRANTED


def test_zone_setup_ok_upload_untouched_and_idle_timer_not_reset():
    harness = Harness.homed()
    harness.reply(ZoneBegin(1, 4))
    harness.reply(ZonePoint(0, 1, 1))
    harness.advance(4999)
    assert harness.reply(FireStop()) == OK
    assert harness.device.state is State.ZONE_SETUP
    assert harness.device.zones.upload_summary() == (1, 4, 1)
    harness.advance(1)  # 5000 ms after the last upload message: the stop did not count
    assert harness.device.state is State.DISARMED


def test_safe_error_ok_and_faults_and_recovery_untouched():
    harness = Harness.homed(key_on=True)
    harness.control.set_key(False)
    before = harness.snapshot()
    assert harness.reply(FireStop()) == OK
    assert harness.snapshot() == before
    harness.control.set_key(True)
    harness.tick()
    assert harness.device.state is State.DISARMED  # recovery was not blocked


# ---- timing and behaviour -----------------------------------------------------------------------


def test_the_stop_switches_off_at_once_and_the_cooldown_counts_from_the_stop():
    harness = ready()
    harness.reply(request(harness))
    run(harness, 1000)
    stopped_at = harness.clock.now_ms()
    harness.reply(FireStop())
    cut_time = harness.sink.named("effector_stopped_by_host")[0].device_ms
    assert 0 <= cut_time - stopped_at <= 100
    run(harness, 4999)
    assert harness.reply(request(harness)) == Reply.denied(DenyReason.COOLDOWN_ACTIVE)
    harness.advance(1)
    assert harness.clock.now_ms() - stopped_at == 5000
    assert harness.reply(request(harness)).result is Result.GRANTED


def test_an_idle_stop_followed_by_an_immediate_request_is_granted():
    harness = ready()
    assert harness.reply(FireStop()) == OK
    assert harness.reply(request(harness)).result is Result.GRANTED


def test_a_duplicate_stop_gives_one_stop_event():
    harness = ready()
    harness.reply(request(harness))
    wire = host_frame(FireStop(), 9000)
    (first,) = harness.send_wire(wire)
    (second,) = harness.send_wire(wire)
    assert second.raw == first.raw
    assert len(stop_events(harness)) == 1


def test_a_stop_with_a_payload_byte_is_bad_length_and_stops_nothing():
    harness = ready()
    harness.reply(request(harness))
    (out,) = harness.send_raw(MsgType.FIRE_STOP, b"\x00")
    assert out.message == Reply.rejected(MsgType.FIRE_STOP, RejectCode.BAD_LENGTH)
    assert harness.device.effector_on is True


def test_a_stop_does_not_refresh_the_heartbeat_timer():
    harness = Harness.homed()
    harness.heartbeat()
    harness.advance(900)
    harness.reply(FireStop())
    harness.advance(101)
    assert harness.device.state is State.SAFE_ERROR  # the original deadline


def test_a_stop_during_a_later_shot_ends_that_shot():
    harness = ready()
    harness.reply(request(harness, 500))
    run(harness, 500 + 5000)
    harness.reply(FireStop())  # arrives after the first shot ended: nothing to stop
    assert harness.device.state is State.SCANNING
    harness.reply(request(harness))
    harness.reply(FireStop())
    assert harness.device.effector_on is False
    assert harness.device.state is State.COOLDOWN


@given(st.sampled_from(list(State)))
@settings(max_examples=40, deadline=None)
def test_a_stop_never_grants_or_switches_anything_on(state):
    harness = Harness.homed(key_on=True)
    harness.force_state(state)
    assert harness.device.effector_on is False
    reply = harness.reply(FireStop())
    assert reply == OK
    assert harness.device.effector_on is False
    assert harness.sink.named("fire_granted") == []
    assert harness.sink.named("effector_on") == []
