"""Tests for the key input, ARM and DISARM (T018)."""

import pytest

from deterrent.protocol.codes import EventType, MsgType, RejectCode, Result, State
from deterrent.protocol.messages import Arm, Disarm, Reply
from deterrent.simulator.control import SimulatorControl
from support.device_harness import Harness


def refused(code: RejectCode) -> Reply:
    return Reply.rejected(MsgType.ARM, code)


def armed_harness() -> Harness:
    harness = Harness.homed(key_on=True)
    assert harness.reply(Arm()).result is Result.OK
    return harness


# ---- the key ----------------------------------------------------------------------------------


def test_key_defaults_to_off_and_is_set_through_the_simulator_control():
    harness = Harness.homed()
    assert harness.device.key_on is False
    SimulatorControl(harness.device).set_key(True)
    assert harness.device.key_on is True
    SimulatorControl(harness.device).set_key(False)
    assert harness.device.key_on is False


def test_key_level_is_reported_in_status():
    harness = Harness.homed()
    harness.control.set_key(True)
    harness.tick()
    assert harness.events_of(EventType.STATUS)[-1].message.status.key_on is True
    harness.control.set_key(False)
    harness.tick()
    assert harness.events_of(EventType.STATUS)[-1].message.status.key_on is False


# ---- ARM --------------------------------------------------------------------------------------


def test_arm_with_the_key_off_is_refused():
    harness = Harness.homed()
    assert harness.reply(Arm()) == refused(RejectCode.KEY_OFF)
    assert harness.device.armed is False
    assert harness.device.state is State.DISARMED


def test_arm_during_boot_is_refused_not_homed():
    harness = Harness(boot_duration_ms=500)
    harness.control.set_key(True)
    assert harness.reply(Arm()) == refused(RejectCode.NOT_HOMED)


def test_arm_during_homing_is_refused_busy_homing():
    harness = Harness()
    harness.control.set_key(True)
    harness.advance(1000)
    assert harness.reply(Arm()) == refused(RejectCode.BUSY_HOMING)
    assert harness.device.armed is False


def test_arm_when_not_homed_after_the_homing_was_lost_is_refused_not_homed():
    harness = Harness.homed(key_on=True)
    harness.device.homed = False
    assert harness.reply(Arm()) == refused(RejectCode.NOT_HOMED)


def test_arm_with_the_key_on_succeeds_and_state_is_scanning():
    harness = Harness.homed(key_on=True)
    reply = harness.reply(Arm())
    assert reply == Reply.ok(MsgType.ARM)
    assert harness.device.armed is True
    assert harness.device.state is State.SCANNING


def test_arm_when_already_armed_is_an_idempotent_ok():
    harness = armed_harness()
    assert harness.reply(Arm()) == Reply.ok(MsgType.ARM)
    assert harness.device.state is State.SCANNING
    assert harness.device.armed is True


def test_arm_with_a_latched_fault_is_refused_fault_active():
    harness = Harness.homed(key_on=True)
    harness.device.fault_mask = 0x0004  # a reserved bit, set directly for this test
    assert harness.reply(Arm()) == refused(RejectCode.FAULT_ACTIVE)
    assert harness.device.armed is False


def test_arm_in_zone_setup_is_wrong_state():
    harness = Harness.homed(key_on=True)
    harness.device.state = State.ZONE_SETUP
    assert harness.reply(Arm()) == refused(RejectCode.WRONG_STATE)
    assert harness.device.state is State.ZONE_SETUP


def test_arm_checks_run_in_the_documented_order():
    # not homed beats key off
    harness = Harness.homed()
    harness.device.homed = False
    assert harness.reply(Arm()) == refused(RejectCode.NOT_HOMED)
    # key off beats a fault
    harness = Harness.homed()
    harness.device.fault_mask = 0x0004
    assert harness.reply(Arm()) == refused(RejectCode.KEY_OFF)
    # a fault beats a wrong state
    harness = Harness.homed(key_on=True)
    harness.device.fault_mask = 0x0004
    harness.device.state = State.ZONE_SETUP
    assert harness.reply(Arm()) == refused(RejectCode.FAULT_ACTIVE)


# ---- DISARM -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    [
        State.DISARMED,
        State.SCANNING,
        State.DETER,
        State.COOLDOWN,
        State.ZONE_SETUP,
        State.SAFE_ERROR,
    ],
)
def test_disarm_replies_ok_in_every_state(state):
    harness = Harness.homed(key_on=True)
    harness.device.state = state
    assert harness.reply(Disarm()) == Reply.ok(MsgType.DISARM)


def test_disarm_replies_ok_in_boot_and_homing():
    boot = Harness(boot_duration_ms=500)
    assert boot.reply(Disarm()) == Reply.ok(MsgType.DISARM)
    homing = Harness()
    homing.advance(500)
    assert homing.reply(Disarm()) == Reply.ok(MsgType.DISARM)


@pytest.mark.parametrize("state", [State.SCANNING, State.COOLDOWN, State.ZONE_SETUP, State.DETER])
def test_disarm_moves_these_states_to_disarmed(state):
    harness = armed_harness()
    harness.device.state = state
    harness.reply(Disarm())
    assert harness.device.state is State.DISARMED
    assert harness.device.armed is False


def test_disarm_in_disarmed_is_a_no_op():
    harness = Harness.homed()
    before = harness.snapshot()
    harness.reply(Disarm())
    assert harness.snapshot() == before


def test_disarm_in_safe_error_changes_nothing():
    harness = Harness.homed()
    harness.device.state = State.SAFE_ERROR
    harness.device.fault_mask = 1
    harness.reply(Disarm())
    assert harness.device.state is State.SAFE_ERROR
    assert harness.device.fault_mask == 1


def test_disarm_during_homing_does_not_abandon_homing():
    harness = Harness()
    harness.advance(1000)
    harness.reply(Disarm())
    assert harness.device.state is State.HOMING
    harness.advance(2000)
    assert harness.device.state is State.DISARMED
    assert harness.device.homed is True


def test_disarm_during_boot_leaves_boot_alone():
    harness = Harness(boot_duration_ms=500)
    harness.reply(Disarm())
    assert harness.device.state is State.BOOT


def test_arm_again_after_disarm_works():
    harness = armed_harness()
    harness.reply(Disarm())
    assert harness.reply(Arm()).result is Result.OK
    assert harness.device.state is State.SCANNING
