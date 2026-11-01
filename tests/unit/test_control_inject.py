"""Tests for the test-only state injection interface (T028)."""

import pytest

from deterrent.clock import FakeClock
from deterrent.config import load_settings
from deterrent.protocol.codes import (
    DenyReason,
    EventType,
    Fault,
    MsgType,
    RejectCode,
    Result,
    State,
)
from deterrent.protocol.messages import FireRequest, Heartbeat, Move, Reply, StatusRequest
from deterrent.simulator.control import InjectionDisabled, SimulatorControl
from deterrent.simulator.device import Device
from deterrent.simulator.zone_storage import StorageMode, StoredZone, ZoneStorage
from support.device_harness import Harness

SQUARE = {"zone_id": 1, "points": [[6000, 2000], [7000, 2000], [7000, 3000], [6000, 3000]]}


def fresh() -> Harness:
    return Harness()  # a device that is still booting; apply_initial replaces that situation


def fire(harness: Harness, duration_ms: int = 500, age_ms: int = 0) -> Reply:
    now = harness.clock.now_ms()
    return harness.reply(FireRequest((now - age_ms) % 2**32, 500, 0, 0, duration_ms))


# ---- the defaults ------------------------------------------------------------------------------


def test_the_default_start_is_booted_homed_disarmed_key_off_no_zones_heartbeat_fresh_and_clear():
    harness = fresh()
    harness.control.apply_initial({})
    device = harness.device
    assert (device.state, device.homed, device.armed, device.key_on) == (
        State.DISARMED,
        True,
        False,
        False,
    )
    assert (device.pan, device.tilt, device.fault_mask, device.effector_on) == (0, 0, 0, False)
    assert harness.storage.read_all() == []
    check = device.person_check()
    assert (check.present, check.delay_elapsed) == (False, True)
    harness.advance(1000)
    assert device.state is State.DISARMED  # the heartbeat is fresh, then goes stale at 1000 ms
    harness.advance(1)
    assert device.state is State.SAFE_ERROR


def test_apply_initial_resets_whatever_was_there_before():
    harness = Harness.homed(key_on=True)
    harness.store_zone(1, SQUARE_POINTS)
    harness.control.apply_initial({})
    assert harness.device.key_on is False
    assert harness.storage.read_all() == []


SQUARE_POINTS = [(6000, 2000), (7000, 2000), (7000, 3000), (6000, 3000)]


# ---- every initial field takes effect -----------------------------------------------------------


@pytest.mark.parametrize(
    ("initial", "expected_state"),
    [
        ({"state": "SCANNING", "armed": True}, State.SCANNING),
        ({"state": "COOLDOWN", "armed": True, "cooldown_remaining_ms": 3000}, State.COOLDOWN),
        ({"state": "ZONE_SETUP"}, State.ZONE_SETUP),
        ({"state": "SAFE_ERROR", "fault_mask": 1}, State.SAFE_ERROR),
        ({"state": "HOMING"}, State.HOMING),
        ({"state": "DISARMED"}, State.DISARMED),
    ],
)
def test_state_is_injected(initial, expected_state):
    harness = fresh()
    harness.control.apply_initial(initial)
    assert harness.device.state is expected_state


def test_homed_armed_key_and_angles_and_fault_mask_are_injected():
    harness = fresh()
    harness.control.apply_initial(
        {
            "state": "SCANNING",
            "homed": True,
            "armed": True,
            "key_on": True,
            "pan_cdeg": 12000,
            "tilt_cdeg": 4000,
        }
    )
    device = harness.device
    assert (device.homed, device.armed, device.key_on) == (True, True, True)
    assert (device.pan, device.tilt) == (12000, 4000)
    harness.control.apply_initial({"homed": False, "fault_mask": 0x0004})
    assert device.homed is False
    assert device.fault_mask == 0x0004


def test_heartbeat_age_sets_how_close_the_watchdog_is():
    harness = fresh()
    harness.control.apply_initial({"heartbeat_age_ms": 600})
    harness.advance(400)
    assert harness.device.state is State.DISARMED  # 1000 ms old: still alive
    harness.advance(1)
    assert harness.device.state is State.SAFE_ERROR


def test_no_heartbeat_means_none_has_been_accepted():
    harness = fresh()
    harness.control.apply_initial(
        {"no_heartbeat": True, "key_on": True, "armed": True, "state": "SCANNING"}
    )
    harness.advance(60_000)
    assert harness.device.state is State.SCANNING  # the watchdog never started
    assert fire(harness) == Reply.denied(DenyReason.HEARTBEAT_LOST)
    assert harness.device.person_check().present is True


def test_person_clear_false_means_a_person_is_present():
    harness = fresh()
    harness.control.apply_initial({"person_clear": False})
    assert harness.device.person_check().present is True


def test_person_clear_age_ms_makes_the_status_stale_past_the_hold_off():
    harness = fresh()
    harness.control.apply_initial({"person_clear_age_ms": 1000})
    assert harness.device.person_check().present is False
    harness.control.apply_initial({"person_clear_age_ms": 1001, "heartbeat_age_ms": 0})
    assert harness.device.person_check().present is True


def test_clear_streak_decides_whether_the_re_engage_delay_has_elapsed():
    harness = fresh()
    harness.control.apply_initial({"clear_streak_ms": 4999})
    assert harness.device.person_check().delay_elapsed is False
    harness.control.apply_initial({"clear_streak_ms": 5000})
    assert harness.device.person_check().delay_elapsed is True


def test_zones_are_stored_with_correct_checksums():
    harness = fresh()
    harness.control.apply_initial({"zones": [SQUARE, {**SQUARE, "zone_id": 2}]})
    assert [zone.zone_id for zone in harness.storage.read_all()] == [1, 2]
    assert harness.device.zones.store_valid() is True
    assert harness.device.status_block().zone_count == 2


def test_zone_store_corrupt_damages_the_stored_bytes_but_not_their_crc():
    harness = fresh()
    harness.control.apply_initial({"zones": [SQUARE], "zone_store_corrupt": True})
    assert harness.device.zones.store_valid() is False
    assert harness.device.fault_mask == 0


def test_zone_store_corrupt_without_a_zone_is_an_error():
    harness = fresh()
    with pytest.raises(ValueError):
        harness.control.apply_initial({"zone_store_corrupt": True})


def test_zone_storage_unreadable_switches_the_mock_mode():
    harness = fresh()
    harness.control.apply_initial({"zone_storage_unreadable": True})
    assert harness.storage.mode is StorageMode.UNREADABLE
    assert harness.device.zones.store_valid() is False
    harness.control.inject({"zone_storage_unreadable": False})
    assert harness.storage.mode is StorageMode.OK


def test_cooldown_remaining_ms_starts_a_running_cooldown_with_that_much_left():
    harness = fresh()
    harness.control.apply_initial({"cooldown_remaining_ms": 3000})
    assert harness.device.cooldown_running() is True
    harness.advance(2999)
    assert harness.device.cooldown_running() is True
    harness.advance(1)
    assert harness.device.cooldown_running() is False


def test_absent_cooldown_remaining_means_no_cooldown():
    harness = fresh()
    harness.control.apply_initial({})
    assert harness.device.cooldown_running() is False


def test_last_seq_makes_older_numbers_stale():
    harness = fresh()
    harness.control.apply_initial({"last_seq": 500})
    harness.send(Heartbeat(True, 0), seq=499)
    assert harness.log[-1].message == Reply.rejected(MsgType.HEARTBEAT, RejectCode.STALE_SEQ)
    out = harness.send(Heartbeat(True, 0), seq=501)
    assert out[0].message.result is Result.OK


def test_unknown_fields_are_refused():
    harness = fresh()
    with pytest.raises(ValueError):
        harness.control.apply_initial({"colour": "red"})


# ---- injection can never grant or switch the effector on ----------------------------------------


ALL_GOOD = {
    "state": "SCANNING",
    "armed": True,
    "key_on": True,
    "zones": [SQUARE],
}


def test_injecting_every_condition_for_a_grant_does_not_grant_anything_by_itself():
    harness = fresh()
    harness.control.apply_initial(ALL_GOOD)
    harness.advance(500)
    assert harness.device.effector_on is False
    assert harness.sink.named("fire_granted") == []
    assert harness.sink.named("effector_on") == []
    assert [o for o in harness.log if o.type == EventType.EFFECTOR_EVENT] == []


def test_a_request_is_still_needed_and_only_then_is_there_a_grant():
    harness = fresh()
    harness.control.apply_initial(ALL_GOOD)
    reply = fire(harness)
    assert reply.result is Result.GRANTED
    assert len(harness.sink.named("fire_granted")) == 1


def test_there_is_no_way_to_inject_the_effector():
    harness = fresh()
    with pytest.raises(ValueError):
        harness.control.apply_initial({"effector_on": True})
    with pytest.raises(ValueError):
        harness.control.inject({"effector": True})
    assert harness.device.effector_on is False
    public = {name for name in dir(SimulatorControl) if not name.startswith("_")}
    assert public == {"apply_initial", "inject", "set_key"}


def test_the_deter_state_cannot_be_injected_because_it_means_the_effector_is_on():
    harness = fresh()
    with pytest.raises(ValueError):
        harness.control.apply_initial({"state": "DETER"})
    assert harness.device.state is State.BOOT


# ---- the guard ----------------------------------------------------------------------------------


def test_with_injection_disabled_every_call_is_refused_and_changes_nothing():
    clock = FakeClock()
    device = Device(clock, load_settings(), allow_test_injection=False)
    control = SimulatorControl(device)
    before = (device.state, device.homed, device.armed, device.key_on, device.fault_mask)
    with pytest.raises(InjectionDisabled):
        control.apply_initial({"state": "SCANNING", "armed": True})
    with pytest.raises(InjectionDisabled):
        control.inject({"armed": True})
    assert (device.state, device.homed, device.armed, device.key_on, device.fault_mask) == before


def test_the_key_input_still_works_with_injection_disabled():
    clock = FakeClock()
    device = Device(clock, load_settings(), allow_test_injection=False)
    SimulatorControl(device).set_key(True)
    assert device.key_on is True


# ---- mid-vector injection -----------------------------------------------------------------------


def test_set_key_off_from_an_injected_armed_state_follows_the_key_off_rule():
    harness = fresh()
    harness.control.apply_initial({"state": "SCANNING", "armed": True, "key_on": True})
    harness.control.set_key(False)
    assert harness.device.state is State.SAFE_ERROR
    assert harness.device.fault_mask == Fault.KEY_OFF
    assert harness.device.armed is False


def test_an_injected_key_change_is_silent_not_a_key_off_event():
    harness = fresh()
    harness.control.apply_initial({"state": "SCANNING", "armed": True, "key_on": True})
    harness.control.inject({"key_on": False})  # the injection sets the level, it is not an event
    assert harness.device.state is State.SCANNING
    assert fire(harness) == Reply.denied(DenyReason.KEY_OFF)


def test_inject_corrupts_the_store_while_a_shot_is_running_and_the_shot_is_cut():
    harness = fresh()
    harness.control.apply_initial(ALL_GOOD)
    assert fire(harness, duration_ms=5000).result is Result.GRANTED
    harness.control.inject({"zone_store_corrupt": True})
    harness.advance(10)
    assert harness.device.effector_on is False
    assert harness.sink.named("effector_cut")[0].details["cause"] == int(
        DenyReason.ZONE_STORE_INVALID
    )


def test_inject_changes_only_the_fields_it_names():
    harness = fresh()
    harness.control.apply_initial({"key_on": True, "pan_cdeg": 700, "zones": [SQUARE]})
    harness.control.inject({"tilt_cdeg": 300})
    assert (harness.device.pan, harness.device.tilt) == (700, 300)
    assert harness.device.key_on is True
    assert len(harness.storage.read_all()) == 1


def test_injected_zones_replace_the_whole_store_on_inject():
    harness = fresh()
    harness.control.apply_initial({"zones": [SQUARE, {**SQUARE, "zone_id": 2}]})
    harness.control.inject({"zones": [{**SQUARE, "zone_id": 3}]})
    assert [zone.zone_id for zone in harness.storage.read_all()] == [3]


def test_apply_initial_needs_a_mock_storage_for_the_failure_modes():
    class PlainStorage(ZoneStorage):
        def read_all(self):
            return []

        def write_zone(self, zone: StoredZone) -> None:
            pass

        def delete_zone(self, zone_id: int) -> None:
            pass

        def clear(self) -> None:
            pass

    device = Device(FakeClock(), load_settings(), storage=PlainStorage(), allow_test_injection=True)
    control = SimulatorControl(device)
    control.apply_initial({})
    with pytest.raises(ValueError):
        control.apply_initial({"zone_storage_unreadable": True})


def test_status_and_move_still_work_after_injection():
    harness = fresh()
    harness.control.apply_initial({"pan_cdeg": 100, "tilt_cdeg": 200})
    assert harness.reply(StatusRequest()).result is Result.OK
    assert harness.reply(Move(1, 1)).result is Result.OK
