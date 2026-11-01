"""Tests for ZONE_DELETE, ZONE_LIST, ZONE_CLEAR, store validity and the ZONE_SETUP exits (T023)."""

import pytest

from deterrent.protocol.codes import EventType, Fault, MsgType, RejectCode, Result, State
from deterrent.protocol.crc import zone_crc32
from deterrent.protocol.messages import (
    Arm,
    Disarm,
    Heartbeat,
    Home,
    Move,
    Reply,
    StatusBlock,
    StatusRequest,
    ZoneBegin,
    ZoneClear,
    ZoneCommit,
    ZoneDelete,
    ZoneList,
    ZonePoint,
    unpack_zone_list,
)
from deterrent.simulator.zone_storage import StorageMode
from support.device_harness import Harness

SQUARE = [(0, 0), (1000, 0), (1000, 1000), (0, 1000)]
OTHER = [(2000, 0), (3000, 0), (3000, 1000), (2000, 1000)]


def store_zone(harness: Harness, zone_id: int, points) -> None:
    assert harness.reply(ZoneBegin(zone_id, len(points))).result is Result.OK
    for index, (pan, tilt) in enumerate(points):
        harness.reply(ZonePoint(index, pan, tilt))
    assert harness.reply(ZoneCommit(zone_crc32(zone_id, points))).result is Result.OK


def validity(harness: Harness) -> bool:
    reply = harness.reply(StatusRequest())
    return StatusBlock.decode_payload(reply.data).zone_store_valid


@pytest.fixture
def harness() -> Harness:
    harness = Harness.homed()
    store_zone(harness, 1, SQUARE)
    store_zone(harness, 2, OTHER)
    return harness


# ---- delete ----------------------------------------------------------------------------------


def test_delete_a_known_zone(harness):
    assert harness.reply(ZoneDelete(1)) == Reply.ok(MsgType.ZONE_DELETE)
    assert [z.zone_id for z in harness.storage.read_all()] == [2]


def test_delete_an_unknown_zone_is_zone_unknown_id_and_changes_nothing(harness):
    before = harness.storage.read_all()
    assert harness.reply(ZoneDelete(9)) == Reply.rejected(
        MsgType.ZONE_DELETE, RejectCode.ZONE_UNKNOWN_ID
    )
    assert harness.storage.read_all() == before


def test_delete_acts_on_a_whole_zone_only(harness):
    harness.reply(ZoneDelete(2))
    (left,) = harness.storage.read_all()
    assert left.zone_id == 1
    assert left.point_count == 4


def test_delete_is_wrong_state_outside_disarmed_and_zone_setup(harness):
    harness.control.set_key(True)
    harness.reply(Arm())
    assert harness.reply(ZoneDelete(1)) == Reply.rejected(
        MsgType.ZONE_DELETE, RejectCode.WRONG_STATE
    )
    assert len(harness.storage.read_all()) == 2


def test_delete_works_inside_zone_setup_and_does_not_leave_it(harness):
    harness.reply(ZoneBegin(5, 3))
    assert harness.reply(ZoneDelete(1)).result is Result.OK
    assert harness.device.state is State.ZONE_SETUP
    assert harness.device.zones.upload_summary() == (5, 3, 0)


def test_delete_reports_a_storage_error(harness):
    harness.storage.mode = StorageMode.LOCKED
    assert harness.reply(ZoneDelete(1)) == Reply.rejected(
        MsgType.ZONE_DELETE, RejectCode.ZONE_STORAGE_ERROR
    )


# ---- clear -----------------------------------------------------------------------------------


def test_clear_in_disarmed_empties_the_store(harness):
    assert harness.reply(ZoneClear()) == Reply.ok(MsgType.ZONE_CLEAR)
    assert harness.storage.read_all() == []


def test_clear_in_zone_setup_empties_the_store_and_keeps_the_upload(harness):
    harness.reply(ZoneBegin(5, 3))
    assert harness.reply(ZoneClear()).result is Result.OK
    assert harness.storage.read_all() == []
    assert harness.device.state is State.ZONE_SETUP
    assert harness.device.zones.upload_summary() == (5, 3, 0)


def test_clear_is_wrong_state_elsewhere(harness):
    harness.control.set_key(True)
    harness.reply(Arm())
    assert harness.reply(ZoneClear()) == Reply.rejected(MsgType.ZONE_CLEAR, RejectCode.WRONG_STATE)


# ---- list ------------------------------------------------------------------------------------


def test_list_matches_the_stored_zones_and_their_crcs(harness):
    reply = harness.reply(ZoneList())
    entries = unpack_zone_list(reply.data)
    assert [(e.zone_id, e.point_count, e.crc32) for e in entries] == [
        (1, 4, zone_crc32(1, SQUARE)),
        (2, 4, zone_crc32(2, OTHER)),
    ]


def test_list_of_an_empty_store(harness):
    harness.reply(ZoneClear())
    assert unpack_zone_list(harness.reply(ZoneList()).data) == []


def test_list_works_in_every_state_except_boot():
    boot = Harness(boot_duration_ms=500)
    assert boot.reply(ZoneList()) == Reply.rejected(MsgType.ZONE_LIST, RejectCode.WRONG_STATE)
    for state in (State.DISARMED, State.SCANNING, State.COOLDOWN, State.ZONE_SETUP, State.HOMING):
        harness = Harness.homed()
        harness.device.state = state
        assert harness.reply(ZoneList()).result is Result.OK, state


def test_list_with_unreadable_storage_is_a_storage_error(harness):
    harness.storage.mode = StorageMode.UNREADABLE
    assert harness.reply(ZoneList()) == Reply.rejected(
        MsgType.ZONE_LIST, RejectCode.ZONE_STORAGE_ERROR
    )


# ---- store validity is derived, never stored -------------------------------------------------


def test_an_empty_store_is_valid():
    assert validity(Harness.homed()) is True


def test_a_store_of_intact_zones_is_valid(harness):
    assert validity(harness) is True
    assert harness.device.status_block().zone_count == 2


def test_corrupt_bytes_make_the_store_invalid_and_it_stays_invalid(harness):
    harness.storage.corrupt_zone(1)
    assert validity(harness) is False
    harness.advance(10_000)
    assert validity(harness) is False


def test_clear_makes_a_corrupt_store_valid_again(harness):
    harness.storage.corrupt_zone(1)
    assert harness.reply(ZoneClear()).result is Result.OK
    assert validity(harness) is True


def test_deleting_the_corrupt_zone_alone_also_restores_validity(harness):
    harness.storage.corrupt_zone(1)
    harness.reply(ZoneDelete(1))
    assert validity(harness) is True


def test_unreadable_storage_is_invalid_and_valid_again_without_a_clear(harness):
    harness.storage.mode = StorageMode.UNREADABLE
    assert validity(harness) is False
    harness.storage.mode = StorageMode.OK
    assert validity(harness) is True
    assert len(harness.storage.read_all()) == 2  # nothing was cleared


def test_unreadable_and_then_corrupt_stays_invalid(harness):
    harness.storage.mode = StorageMode.UNREADABLE
    harness.storage.corrupt_zone(2)
    assert validity(harness) is False
    harness.storage.mode = StorageMode.OK
    assert validity(harness) is False


def test_validity_is_a_status_value_not_a_fault_bit(harness):
    harness.storage.corrupt_zone(1)
    harness.tick()
    status = harness.events_of(EventType.STATUS)[-1].message.status
    assert status.zone_store_valid is False
    assert status.fault_mask == 0
    assert harness.device.fault_mask == 0


def test_status_events_follow_a_validity_change(harness):
    harness.tick()
    assert harness.events_of(EventType.STATUS)[-1].message.status.zone_store_valid is True
    harness.storage.corrupt_zone(1)
    harness.tick()
    assert harness.events_of(EventType.STATUS)[-1].message.status.zone_store_valid is False


# ---- leaving ZONE_SETUP: one test per row of the table -------------------------------------


def start_upload(harness: Harness) -> tuple:
    harness.reply(ZoneBegin(7, 4))
    harness.reply(ZonePoint(0, 1, 1))
    assert harness.device.state is State.ZONE_SETUP
    return tuple(harness.storage.read_all())


def assert_exit(harness: Harness, state: State, stored: tuple) -> None:
    assert harness.device.state is state
    assert tuple(harness.storage.read_all()) == stored  # stored zones unchanged
    assert harness.device.zones.upload_summary() is None  # partial upload discarded
    assert harness.device.effector_on is False


def test_row_in_begin_accepted_from_disarmed(harness):
    stored = tuple(harness.storage.read_all())
    assert harness.reply(ZoneBegin(7, 4)).result is Result.OK
    assert harness.device.state is State.ZONE_SETUP
    assert tuple(harness.storage.read_all()) == stored
    assert harness.device.zones.upload_summary() == (7, 4, 0)
    assert harness.device.effector_on is False


def test_row_1_commit_accepted(harness):
    harness.reply(ZoneBegin(7, 4))
    for index, (pan, tilt) in enumerate(SQUARE):
        harness.reply(ZonePoint(index, pan + 5000, tilt))
    shifted = [(pan + 5000, tilt) for pan, tilt in SQUARE]
    harness.reply(ZoneCommit(zone_crc32(7, shifted)))
    assert harness.device.state is State.DISARMED
    assert [z.zone_id for z in harness.storage.read_all()] == [1, 2, 7]
    assert harness.device.zones.upload_summary() is None


def test_row_2_commit_rejected(harness):
    stored = start_upload(harness)
    assert harness.reply(ZoneCommit(0)).result is Result.REJECTED
    assert_exit(harness, State.DISARMED, stored)


def test_row_3_idle_timeout(harness):
    stored = start_upload(harness)
    harness.advance(5000)
    assert_exit(harness, State.DISARMED, stored)


def test_row_4_disarm(harness):
    stored = start_upload(harness)
    assert harness.reply(Disarm()).result is Result.OK
    assert_exit(harness, State.DISARMED, stored)


def test_row_5_key_turned_off(harness):
    harness.control.set_key(True)
    stored = start_upload(harness)
    harness.control.set_key(False)
    assert_exit(harness, State.SAFE_ERROR, stored)
    assert harness.device.fault_mask == Fault.KEY_OFF


def test_row_6_heartbeat_lost(harness):
    harness.heartbeat()
    stored = start_upload(harness)
    harness.advance(1001)
    assert_exit(harness, State.SAFE_ERROR, stored)
    assert harness.device.fault_mask == Fault.HEARTBEAT_LOST


def test_row_7_begin_accepted_while_in_zone_setup_starts_fresh(harness):
    stored = start_upload(harness)
    assert harness.reply(ZoneBegin(8, 3)).result is Result.OK
    assert harness.device.state is State.ZONE_SETUP
    assert harness.device.zones.upload_summary() == (8, 3, 0)
    assert tuple(harness.storage.read_all()) == stored


def test_row_8_begin_rejected_changes_nothing(harness):
    start_upload(harness)
    harness.reply(ZoneBegin(9, 2))
    assert harness.device.state is State.ZONE_SETUP
    assert harness.device.zones.upload_summary() == (7, 4, 1)
    # ...and a rejected begin from DISARMED does not enter ZONE_SETUP.
    other = Harness.homed()
    other.reply(ZoneBegin(9, 2))
    assert other.device.state is State.DISARMED


def test_messages_that_do_not_leave_zone_setup(harness):
    start_upload(harness)
    assert harness.reply(ZonePoint(5, 1, 1)).result is Result.REJECTED  # bad index
    assert harness.reply(ZoneDelete(1)).result is Result.OK
    assert harness.reply(ZoneList()).result is Result.OK
    assert harness.reply(Move(10, 10)).result is Result.OK
    assert harness.reply(Heartbeat(True, 0)).result is Result.OK
    assert harness.reply(StatusRequest()).result is Result.OK
    assert harness.reply(ZoneClear()).result is Result.OK
    assert harness.device.state is State.ZONE_SETUP
    assert harness.device.zones.upload_summary() == (7, 4, 1)


def test_arm_and_home_are_wrong_state_in_zone_setup_and_change_nothing(harness):
    harness.control.set_key(True)
    start_upload(harness)
    assert harness.reply(Arm()) == Reply.rejected(MsgType.ARM, RejectCode.WRONG_STATE)
    assert harness.reply(Home()) == Reply.rejected(MsgType.HOME, RejectCode.WRONG_STATE)
    assert harness.device.state is State.ZONE_SETUP
    assert harness.device.zones.upload_summary() == (7, 4, 1)
