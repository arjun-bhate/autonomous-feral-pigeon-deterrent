"""Tests for the heartbeat, the watchdog and SAFE_ERROR (T019a, T019b)."""

import pytest

from deterrent.protocol.codes import EventType, Fault, MsgType, RejectCode, Result, State
from deterrent.protocol.messages import (
    Arm,
    Heartbeat,
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
