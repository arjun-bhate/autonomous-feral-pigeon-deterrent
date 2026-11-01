"""Tests for HOME, MOVE and JOG (T017)."""

import pytest

from deterrent.protocol.codes import EventType, MsgType, RejectCode, Result, State
from deterrent.protocol.messages import Home, Jog, Move, Reply, unpack_angles
from support.device_harness import Harness


def refused(msg_type: MsgType, code: RejectCode) -> Reply:
    return Reply.rejected(msg_type, code)


# ---- while BOOT or HOMING -------------------------------------------------------------------


def test_move_and_jog_during_boot_are_refused_not_homed_and_nothing_moves():
    harness = Harness(boot_duration_ms=500)
    assert harness.device.state is State.BOOT
    assert harness.reply(Move(100, 100)) == refused(MsgType.MOVE, RejectCode.NOT_HOMED)
    assert harness.reply(Jog(10, 10)) == refused(MsgType.JOG, RejectCode.NOT_HOMED)
    assert (harness.device.pan, harness.device.tilt) == (0, 0)


def test_move_and_jog_during_homing_are_refused_busy_homing_and_nothing_moves():
    harness = Harness()
    harness.advance(1000)
    assert harness.device.state is State.HOMING
    assert harness.reply(Move(100, 100)) == refused(MsgType.MOVE, RejectCode.BUSY_HOMING)
    assert harness.reply(Jog(10, 10)) == refused(MsgType.JOG, RejectCode.BUSY_HOMING)
    assert (harness.device.pan, harness.device.tilt) == (0, 0)


def test_home_during_boot_or_homing_is_refused():
    boot = Harness(boot_duration_ms=500)
    assert boot.reply(Home()) == refused(MsgType.HOME, RejectCode.WRONG_STATE)
    homing = Harness()
    homing.advance(1000)
    assert homing.reply(Home()) == refused(MsgType.HOME, RejectCode.BUSY_HOMING)
    assert homing.device.state is State.HOMING


# ---- MOVE -------------------------------------------------------------------------------------


def test_move_after_homing_is_applied_instantly_and_reported():
    harness = Harness.homed()
    reply = harness.reply(Move(6000, 2000))
    assert reply.result is Result.OK
    assert unpack_angles(reply.data) == (6000, 2000)
    assert (harness.device.pan, harness.device.tilt) == (6000, 2000)
    assert harness.sink.named("angles_changed")[-1].details == {"pan": 6000, "tilt": 2000}


def test_status_reports_the_new_angles():
    harness = Harness.homed()
    harness.reply(Move(6000, 2000))
    status = harness.events_of(EventType.STATUS)[-1].message.status
    assert (status.pan, status.tilt) == (6000, 2000)


@pytest.mark.parametrize(("pan", "tilt"), [(0, 0), (24000, 0), (0, 8000), (24000, 8000)])
def test_move_exactly_at_each_limit_is_accepted(pan, tilt):
    harness = Harness.homed()
    assert harness.reply(Move(pan, tilt)).result is Result.OK
    assert (harness.device.pan, harness.device.tilt) == (pan, tilt)


@pytest.mark.parametrize(("pan", "tilt"), [(24001, 0), (-1, 0), (0, 8001), (0, -1), (30000, 9000)])
def test_move_one_beyond_a_limit_is_refused_and_never_clamped(pan, tilt):
    harness = Harness.homed()
    harness.reply(Move(100, 200))
    reply = harness.reply(Move(pan, tilt))
    assert reply == refused(MsgType.MOVE, RejectCode.OUT_OF_SOFT_LIMIT)
    assert (harness.device.pan, harness.device.tilt) == (100, 200)


# ---- JOG --------------------------------------------------------------------------------------


def test_jog_moves_by_the_given_amount():
    harness = Harness.homed()
    harness.reply(Move(6000, 2000))
    reply = harness.reply(Jog(100, -50))
    assert unpack_angles(reply.data) == (6100, 1950)


def test_jog_to_exactly_a_limit_is_accepted():
    harness = Harness.homed()
    harness.reply(Move(23999, 7999))
    assert harness.reply(Jog(1, 1)).result is Result.OK
    assert (harness.device.pan, harness.device.tilt) == (24000, 8000)


def test_a_jog_that_would_leave_a_limit_is_refused_whole():
    harness = Harness.homed()
    harness.reply(Move(24000, 100))
    reply = harness.reply(Jog(1, 50))  # the pan part is out, the tilt part alone would be fine
    assert reply == refused(MsgType.JOG, RejectCode.OUT_OF_SOFT_LIMIT)
    assert (harness.device.pan, harness.device.tilt) == (24000, 100)
    assert harness.reply(Jog(0, -101)) == refused(MsgType.JOG, RejectCode.OUT_OF_SOFT_LIMIT)
    assert (harness.device.pan, harness.device.tilt) == (24000, 100)


def test_a_zero_jog_changes_nothing_and_reports_no_angle_event():
    harness = Harness.homed()
    before = len(harness.sink.named("angles_changed"))
    assert harness.reply(Jog(0, 0)).result is Result.OK
    assert len(harness.sink.named("angles_changed")) == before


# ---- other states -----------------------------------------------------------------------------


def test_motion_is_refused_wrong_state_in_safe_error():
    harness = Harness.homed()
    harness.device.state = State.SAFE_ERROR  # state forced for this test
    harness.device.fault_mask = 1  # SAFE_ERROR always has a latched fault
    assert harness.reply(Move(1, 1)) == refused(MsgType.MOVE, RejectCode.WRONG_STATE)
    assert harness.reply(Jog(1, 1)) == refused(MsgType.JOG, RejectCode.WRONG_STATE)


def test_motion_while_disarmed_but_not_homed_is_refused_not_homed():
    harness = Harness.homed()
    harness.device.homed = False  # the situation after a key-off during homing
    assert harness.reply(Move(1, 1)) == refused(MsgType.MOVE, RejectCode.NOT_HOMED)


def test_motion_works_in_scanning_and_zone_setup():
    for state in (State.SCANNING, State.ZONE_SETUP, State.COOLDOWN):
        harness = Harness.homed()
        harness.device.state = state
        assert harness.reply(Move(500, 500)).result is Result.OK


# ---- HOME -------------------------------------------------------------------------------------


def test_home_is_wrong_state_outside_disarmed():
    for state in (State.SCANNING, State.DETER, State.COOLDOWN, State.ZONE_SETUP, State.SAFE_ERROR):
        harness = Harness.homed()
        harness.device.state = state
        harness.device.fault_mask = 1 if state is State.SAFE_ERROR else 0
        assert harness.reply(Home()) == refused(MsgType.HOME, RejectCode.WRONG_STATE)
        assert harness.device.state is state


def test_home_in_disarmed_enters_homing_and_ends_disarmed_homed():
    harness = Harness.homed()
    harness.reply(Move(500, 600))
    reply = harness.reply(Home())
    assert reply == Reply.ok(MsgType.HOME)
    assert harness.device.state is State.HOMING
    assert harness.device.homed is False
    harness.advance(2999)
    assert harness.device.state is State.HOMING
    harness.advance(1)
    assert harness.device.state is State.DISARMED
    assert harness.device.homed is True
    assert (harness.device.pan, harness.device.tilt) == (0, 0)


def test_home_emits_exactly_one_more_drift_event():
    harness = Harness.homed()
    assert len(harness.sink.named("homing_drift")) == 1
    harness.reply(Home())
    harness.advance(3000)
    harness.advance(3000)
    assert len(harness.sink.named("homing_drift")) == 2


def test_motion_is_refused_while_rehoming():
    harness = Harness.homed()
    harness.reply(Home())
    assert harness.reply(Move(1, 1)) == refused(MsgType.MOVE, RejectCode.BUSY_HOMING)


def test_home_can_be_repeated_after_it_finishes():
    harness = Harness.homed()
    for _ in range(2):
        assert harness.reply(Home()).result is Result.OK
        harness.advance(3000)
        assert harness.device.state is State.DISARMED
