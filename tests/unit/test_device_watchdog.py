"""Tests for the heartbeat, the watchdog and SAFE_ERROR (T019a, T019b)."""

import pytest

from deterrent.protocol.codes import EventType, Fault, MsgType, RejectCode, Result, State
from deterrent.protocol.messages import (
    Arm,
    Disarm,
    Heartbeat,
    Home,
    Move,
    Reply,
    host_frame,
    unpack_heartbeat_data,
)
from support.device_harness import Harness

WATCHDOG = 1000


def armed_with_heartbeat() -> Harness:
    harness = Harness.homed(key_on=True)
    assert harness.reply(Arm()).result is Result.OK
    harness.heartbeat()
    return harness


# ---- T019a: heartbeat reply and watchdog timing ---------------------------------------------


def test_heartbeat_reply_carries_device_ms_and_state():
    harness = Harness.homed()
    harness.clock.advance(1234)
    reply = harness.heartbeat()
    assert reply.result is Result.OK
    device_ms, state = unpack_heartbeat_data(reply.data)
    assert device_ms == harness.clock.now_ms()
    assert state is State.DISARMED


def test_heartbeat_in_boot_is_refused_and_does_not_start_the_watchdog():
    harness = Harness(boot_duration_ms=100_000)
    reply = harness.reply(Heartbeat(True, 0))
    assert reply == Reply.rejected(MsgType.HEARTBEAT, RejectCode.WRONG_STATE)
    harness.clock.advance(50_000)
    harness.tick()
    assert harness.device.state is State.BOOT


def test_silence_of_exactly_watchdog_ms_is_alive_and_one_more_ms_is_safe_error():
    harness = Harness.homed()
    harness.heartbeat()
    harness.advance(WATCHDOG)
    assert harness.device.state is State.DISARMED
    harness.advance(1)
    assert harness.device.state is State.SAFE_ERROR


def test_a_heartbeat_before_the_first_one_does_not_start_the_watchdog():
    harness = Harness.homed()
    harness.advance(60_000)  # no heartbeat has ever arrived
    assert harness.device.state is State.DISARMED
    assert harness.device.fault_mask == 0


def test_watchdog_entry_latches_the_fault_and_counts_one_interlock_event():
    harness = Harness.homed()
    harness.heartbeat()
    harness.advance(WATCHDOG + 1)
    harness.advance(5000)
    assert harness.device.fault_mask == Fault.HEARTBEAT_LOST
    events = harness.sink.named("interlock_event")
    assert [e.details["cause"].name for e in events] == ["HEARTBEAT_LOST"]
    status = harness.events_of(EventType.STATUS)[-1].message.status
    assert status.fault_mask == int(Fault.HEARTBEAT_LOST)
    assert status.state is State.SAFE_ERROR


def test_motion_is_refused_in_safe_error():
    harness = Harness.homed()
    harness.heartbeat()
    harness.advance(WATCHDOG + 1)
    assert harness.reply(Move(5, 5)) == Reply.rejected(MsgType.MOVE, RejectCode.WRONG_STATE)


def test_a_new_accepted_heartbeat_clears_heartbeat_lost_when_it_is_the_only_fault():
    harness = Harness.homed()
    harness.heartbeat()
    harness.advance(WATCHDOG + 1)
    assert harness.device.state is State.SAFE_ERROR
    reply = harness.heartbeat()
    assert reply.result is Result.OK
    assert harness.device.fault_mask == 0
    assert harness.device.state is State.DISARMED
    assert harness.device.armed is False


def test_the_watchdog_restarts_after_recovery():
    harness = Harness.homed()
    harness.heartbeat()
    harness.advance(WATCHDOG + 1)
    harness.heartbeat()
    harness.advance(WATCHDOG)
    assert harness.device.state is State.DISARMED
    harness.advance(1)
    assert harness.device.state is State.SAFE_ERROR


def test_armed_is_false_in_the_status_after_entering_safe_error_from_an_armed_state():
    harness = armed_with_heartbeat()
    assert harness.device.armed is True
    harness.advance(WATCHDOG + 1)
    assert harness.device.state is State.SAFE_ERROR
    assert harness.device.armed is False
    status = harness.events_of(EventType.STATUS)[-1].message.status
    assert (status.state, status.armed) == (State.SAFE_ERROR, False)


@pytest.mark.parametrize(
    "state", [State.DISARMED, State.SCANNING, State.COOLDOWN, State.ZONE_SETUP]
)
def test_the_watchdog_applies_in_every_state_after_the_first_heartbeat(state):
    harness = Harness.homed()
    harness.heartbeat()
    harness.device.state = state
    harness.advance(WATCHDOG + 1)
    assert harness.device.state is State.SAFE_ERROR


def test_a_stale_heartbeat_and_a_duplicate_heartbeat_do_not_refresh_the_timer():
    harness = Harness.homed()
    harness.reply(Heartbeat(True, 0), seq=10)  # accepted at time T
    harness.advance(600)
    harness.send_wire(host_frame(Heartbeat(True, 0), 10))  # duplicate: replayed
    harness.send_wire(host_frame(Heartbeat(True, 0), 4))  # stale: refused
    harness.advance(399)  # T + 999
    assert harness.device.state is State.DISARMED
    harness.advance(1)  # T + 1000: still alive
    assert harness.device.state is State.DISARMED
    harness.advance(1)  # T + 1001: the original deadline has passed
    assert harness.device.state is State.SAFE_ERROR


def test_a_rejected_heartbeat_does_not_refresh_the_timer():
    harness = Harness.homed()
    harness.heartbeat()
    harness.advance(600)
    harness.send_raw(MsgType.HEARTBEAT, b"\x02\x00\x00\x00\x00")  # FIELD_RANGE
    harness.advance(401)
    assert harness.device.state is State.SAFE_ERROR


# ---- owner decision 1: heartbeat loss during HOMING ---------------------------------------


def test_heartbeat_loss_during_homing_enters_safe_error_and_abandons_homing():
    harness = Harness()
    harness.advance(100)
    assert harness.device.state is State.HOMING
    harness.heartbeat()
    harness.advance(WATCHDOG + 1)
    assert harness.device.state is State.SAFE_ERROR
    assert harness.device.homed is False
    assert harness.device.fault_mask == Fault.HEARTBEAT_LOST
    harness.advance(6000)  # homing would have finished long ago
    assert harness.device.state is State.SAFE_ERROR
    assert harness.device.homed is False
    assert harness.sink.named("homing_drift") == []


def test_heartbeats_during_homing_keep_it_alive_and_homing_completes():
    harness = Harness()
    for _ in range(10):
        harness.advance(300)
        harness.heartbeat()
    assert harness.device.state is State.DISARMED
    assert harness.device.homed is True


def test_recovery_after_heartbeat_loss_in_homing_lands_disarmed_not_homed():
    harness = Harness()
    harness.advance(100)
    harness.heartbeat()
    harness.advance(WATCHDOG + 1)
    harness.heartbeat()
    assert harness.device.state is State.DISARMED
    assert harness.device.homed is False
    harness.control.set_key(True)
    assert harness.reply(Arm()) == Reply.rejected(MsgType.ARM, RejectCode.NOT_HOMED)


# ---- T019b: key off and recovery -----------------------------------------------------------


def test_key_turned_off_in_any_state_but_safe_error_enters_safe_error():
    for state in (State.DISARMED, State.SCANNING, State.COOLDOWN, State.ZONE_SETUP):
        harness = Harness.homed(key_on=True)
        harness.device.state = state
        harness.device.armed = state is not State.DISARMED
        harness.control.set_key(False)
        assert harness.device.state is State.SAFE_ERROR, state
        assert harness.device.fault_mask == Fault.KEY_OFF
        assert harness.device.armed is False
        causes = [e.details["cause"].name for e in harness.sink.named("interlock_event")]
        assert causes == ["KEY_OFF"]


def test_key_off_at_boot_is_not_an_event():
    harness = Harness.homed()  # key defaults to off
    assert harness.device.state is State.DISARMED
    assert harness.device.fault_mask == 0
    assert harness.sink.named("interlock_event") == []
    harness.control.set_key(False)  # off to off is not a change
    assert harness.device.state is State.DISARMED


def test_key_off_before_boot_ends_is_not_an_event():
    harness = Harness(boot_duration_ms=500)
    harness.control.set_key(True)
    harness.control.set_key(False)
    assert harness.device.state is State.BOOT
    assert harness.device.fault_mask == 0


def test_key_off_while_already_in_safe_error_adds_no_fault():
    harness = Harness.homed()
    harness.heartbeat()
    harness.advance(WATCHDOG + 1)
    harness.control.set_key(True)
    harness.control.set_key(False)
    assert harness.device.fault_mask == Fault.HEARTBEAT_LOST


def test_a_heartbeat_alone_does_not_clear_a_key_fault():
    harness = Harness.homed(key_on=True)
    harness.heartbeat()
    harness.control.set_key(False)
    harness.heartbeat()
    harness.advance(100)
    assert harness.device.state is State.SAFE_ERROR
    assert harness.device.fault_mask == Fault.KEY_OFF


def test_the_key_alone_does_not_clear_a_heartbeat_fault():
    harness = Harness.homed(key_on=True)
    harness.heartbeat()
    harness.advance(WATCHDOG + 1)
    harness.control.set_key(True)
    harness.advance(100)
    assert harness.device.state is State.SAFE_ERROR
    assert harness.device.fault_mask == Fault.HEARTBEAT_LOST


def test_key_back_on_clears_a_key_fault_with_no_other_condition():
    harness = Harness.homed(key_on=True)
    harness.control.set_key(False)
    harness.control.set_key(True)
    harness.tick()
    assert harness.device.state is State.DISARMED
    assert harness.device.fault_mask == 0
    assert harness.device.armed is False


def test_both_faults_need_both_conditions_in_either_order():
    for key_first in (True, False):
        harness = Harness.homed(key_on=True)
        harness.heartbeat()
        harness.control.set_key(False)  # KEY_OFF latched
        harness.advance(WATCHDOG + 1)  # HEARTBEAT_LOST latched too
        assert harness.device.fault_mask == Fault.KEY_OFF | Fault.HEARTBEAT_LOST
        if key_first:
            harness.control.set_key(True)
            harness.tick()
            assert harness.device.state is State.SAFE_ERROR
            harness.heartbeat()
        else:
            harness.heartbeat()
            assert harness.device.state is State.SAFE_ERROR
            harness.control.set_key(True)
            harness.tick()
        assert harness.device.state is State.DISARMED
        assert harness.device.fault_mask == 0


def test_recovery_never_rearms():
    harness = armed_with_heartbeat()
    harness.control.set_key(False)
    harness.control.set_key(True)
    harness.tick()
    assert harness.device.state is State.DISARMED
    assert harness.device.armed is False
    assert harness.reply(Arm()).result is Result.OK


def test_key_off_during_homing_enters_safe_error_with_homed_false_and_needs_home_after():
    harness = Harness()
    harness.control.set_key(True)
    harness.advance(100)
    assert harness.device.state is State.HOMING
    harness.control.set_key(False)
    assert harness.device.state is State.SAFE_ERROR
    assert harness.device.homed is False
    assert harness.device.fault_mask == Fault.KEY_OFF
    harness.advance(5000)  # homing does not carry on
    assert harness.device.state is State.SAFE_ERROR
    harness.control.set_key(True)
    harness.tick()
    assert harness.device.state is State.DISARMED
    assert harness.device.homed is False
    assert harness.reply(Arm()) == Reply.rejected(MsgType.ARM, RejectCode.NOT_HOMED)
    assert harness.reply(Move(1, 1)) == Reply.rejected(MsgType.MOVE, RejectCode.NOT_HOMED)
    # HOME is the way back.
    assert harness.reply(Home()).result is Result.OK
    harness.advance(3000)
    assert harness.device.homed is True
    assert harness.reply(Arm()).result is Result.OK


def test_key_off_and_heartbeat_loss_during_homing_recover_only_with_both():
    harness = Harness()
    harness.control.set_key(True)
    harness.advance(100)
    harness.heartbeat()
    harness.control.set_key(False)
    harness.advance(WATCHDOG + 1)
    assert harness.device.fault_mask == Fault.KEY_OFF | Fault.HEARTBEAT_LOST
    harness.control.set_key(True)
    harness.heartbeat()
    assert harness.device.state is State.DISARMED
    assert harness.device.homed is False


def test_disarm_in_safe_error_replies_ok_and_changes_nothing():
    harness = Harness.homed(key_on=True)
    harness.control.set_key(False)
    before = harness.snapshot()
    assert harness.reply(Disarm()).result is Result.OK
    assert harness.snapshot() == before


def test_reserved_fault_bits_2_and_3_are_never_set_by_the_device():
    harness = Harness.homed(key_on=True)
    harness.heartbeat()
    for _ in range(3):
        harness.control.set_key(False)
        harness.advance(WATCHDOG + 1)
        harness.control.set_key(True)
        harness.heartbeat()
        harness.tick()
        assert harness.device.fault_mask & (Fault.HOMING_FAILED | Fault.INTERNAL) == 0
        assert harness.device.fault_mask & Fault.UNUSED_BIT_1 == 0


def test_a_reserved_fault_bit_injected_by_a_test_never_clears():
    harness = Harness.homed(key_on=True)
    harness.heartbeat()
    harness.control.set_key(False)
    harness.device.fault_mask |= Fault.HOMING_FAILED  # injected directly for this test
    harness.control.set_key(True)
    harness.heartbeat()
    assert harness.device.state is State.SAFE_ERROR
    assert harness.device.fault_mask == Fault.HOMING_FAILED
