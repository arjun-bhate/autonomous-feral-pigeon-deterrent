"""Tests for ZONE_COMMIT and its nine ordered checks (T022)."""

import struct

import pytest

from deterrent.protocol.codes import MsgType, RejectCode, Result, State
from deterrent.protocol.crc import crc32, zone_crc32
from deterrent.protocol.messages import Arm, Reply, ZoneBegin, ZoneCommit, ZonePoint
from deterrent.simulator.zone_storage import StorageMode, StoredZone
from deterrent.simulator.zones import ZoneService, _Upload
from support.device_harness import Harness

SQUARE = [(0, 0), (1000, 0), (1000, 1000), (0, 1000)]
OTHER_SQUARE = [(2000, 0), (3000, 0), (3000, 1000), (2000, 1000)]
# Edges cross, but the net signed area is not zero (a symmetric bow-tie has zero net area).
BOW_TIE = [(0, 0), (1000, 1000), (1000, 0), (0, 500)]
SYMMETRIC_BOW_TIE = [(0, 0), (1000, 1000), (1000, 0), (0, 1000)]
COLLINEAR = [(0, 0), (500, 0), (1000, 0)]
REPEATED = [(0, 0), (1000, 0), (1000, 0), (1000, 1000)]


def refused(code: RejectCode) -> Reply:
    return Reply.rejected(MsgType.ZONE_COMMIT, code)


def upload(harness: Harness, zone_id: int, points, *, send_points: int | None = None) -> None:
    assert harness.reply(ZoneBegin(zone_id, len(points))).result is Result.OK
    for index, (pan, tilt) in enumerate(points[:send_points]):
        assert harness.reply(ZonePoint(index, pan, tilt)).result is Result.OK


def commit(harness: Harness, zone_id: int, points, *, crc: int | None = None) -> Reply:
    value = zone_crc32(zone_id, points) if crc is None else crc
    return harness.reply(ZoneCommit(value))


def store_zone(harness: Harness, zone_id: int, points) -> None:
    upload(harness, zone_id, points)
    assert commit(harness, zone_id, points).result is Result.OK


@pytest.fixture
def harness() -> Harness:
    return Harness.homed()


def assert_rejected_cleanly(harness: Harness, reply: Reply, code: RejectCode, before) -> None:
    assert reply == refused(code)
    assert harness.storage.read_all() == before  # nothing stored, old zones byte-for-byte equal
    assert harness.device.state is State.DISARMED
    assert harness.device.zones.upload_summary() is None


# ---- the happy path --------------------------------------------------------------------------


def test_a_valid_commit_is_stored_whole(harness):
    upload(harness, 3, SQUARE)
    reply = commit(harness, 3, SQUARE)
    assert reply == Reply.ok(MsgType.ZONE_COMMIT)
    blob = struct.pack("<hhhhhhhh", *[v for point in SQUARE for v in point])
    assert harness.storage.read_all() == [
        StoredZone(3, zone_crc32(3, SQUARE), 4, blob),
    ]
    assert harness.device.state is State.DISARMED
    assert harness.device.zones.upload_summary() is None


def test_the_stored_crc_is_the_crc_of_id_count_and_points(harness):
    store_zone(harness, 5, SQUARE)
    (zone,) = harness.storage.read_all()
    assert zone.crc32 == crc32(bytes([5, 4]) + zone.points)


def test_two_zones_can_be_stored(harness):
    store_zone(harness, 1, SQUARE)
    store_zone(harness, 2, OTHER_SQUARE)
    assert [z.zone_id for z in harness.storage.read_all()] == [1, 2]


# ---- one test per rejection code, old zones untouched -------------------------------------------


def test_commit_with_no_upload_is_zone_no_upload(harness):
    store_zone(harness, 1, SQUARE)
    before = harness.storage.read_all()
    assert_rejected_cleanly(
        harness, commit(harness, 2, OTHER_SQUARE), RejectCode.ZONE_NO_UPLOAD, before
    )


def test_commit_outside_zone_setup_without_an_upload_changes_no_state(harness):
    harness.control.set_key(True)
    harness.reply(Arm())
    assert commit(harness, 1, SQUARE) == refused(RejectCode.ZONE_NO_UPLOAD)
    assert harness.device.state is State.SCANNING


def test_commit_before_all_points_arrived_is_zone_incomplete(harness):
    store_zone(harness, 1, SQUARE)
    before = harness.storage.read_all()
    upload(harness, 2, OTHER_SQUARE, send_points=3)
    assert_rejected_cleanly(
        harness, commit(harness, 2, OTHER_SQUARE), RejectCode.ZONE_INCOMPLETE, before
    )


def test_wrong_crc_is_zone_bad_checksum(harness):
    store_zone(harness, 1, SQUARE)
    before = harness.storage.read_all()
    upload(harness, 2, OTHER_SQUARE)
    reply = commit(harness, 2, OTHER_SQUARE, crc=zone_crc32(2, OTHER_SQUARE) ^ 1)
    assert_rejected_cleanly(harness, reply, RejectCode.ZONE_BAD_CHECKSUM, before)


def test_fewer_than_three_points_is_zone_too_few_points():
    # BEGIN already refuses this, so the upload is built directly to prove the commit check.
    other = Harness()
    storage = other.storage
    service = ZoneService(storage, other.settings)
    service._upload = _Upload(7, 2, [(0, 0), (10, 10)])
    assert service.commit(zone_crc32(7, [(0, 0), (10, 10)])) == RejectCode.ZONE_TOO_FEW_POINTS
    assert storage.read_all() == []
    assert not service.upload_active


@pytest.mark.parametrize("points", [REPEATED, COLLINEAR])
def test_repeated_points_or_zero_area_is_zone_degenerate(harness, points):
    store_zone(harness, 1, SQUARE)
    before = harness.storage.read_all()
    upload(harness, 2, points)
    assert_rejected_cleanly(harness, commit(harness, 2, points), RejectCode.ZONE_DEGENERATE, before)


def test_a_perfectly_symmetric_bow_tie_has_zero_area_so_check_5_reports_it_first(harness):
    upload(harness, 2, SYMMETRIC_BOW_TIE)
    assert commit(harness, 2, SYMMETRIC_BOW_TIE) == refused(RejectCode.ZONE_DEGENERATE)


def test_crossing_edges_is_zone_self_crossing(harness):
    store_zone(harness, 1, SQUARE)
    before = harness.storage.read_all()
    upload(harness, 2, BOW_TIE)
    assert_rejected_cleanly(
        harness, commit(harness, 2, BOW_TIE), RejectCode.ZONE_SELF_CROSSING, before
    )


def test_an_id_already_stored_is_zone_duplicate_id(harness):
    store_zone(harness, 1, SQUARE)
    before = harness.storage.read_all()
    upload(harness, 1, OTHER_SQUARE)
    assert_rejected_cleanly(
        harness, commit(harness, 1, OTHER_SQUARE), RejectCode.ZONE_DUPLICATE_ID, before
    )


def test_a_store_that_filled_up_during_the_upload_is_zone_store_full(harness):
    for zone_id in range(7):
        harness.storage.write_zone(StoredZone(zone_id, 0, 3, bytes(12)))
    upload(harness, 20, SQUARE)  # room for exactly one more at BEGIN
    harness.storage.write_zone(StoredZone(30, 0, 3, bytes(12)))  # then it fills
    before = harness.storage.read_all()
    assert_rejected_cleanly(
        harness, commit(harness, 20, SQUARE), RejectCode.ZONE_STORE_FULL, before
    )


def test_a_storage_failure_is_zone_storage_error(harness):
    store_zone(harness, 1, SQUARE)
    before = harness.storage.read_all()
    upload(harness, 2, OTHER_SQUARE)
    harness.storage.mode = StorageMode.LOCKED
    reply = commit(harness, 2, OTHER_SQUARE)
    harness.storage.mode = StorageMode.OK
    assert_rejected_cleanly(harness, reply, RejectCode.ZONE_STORAGE_ERROR, before)


@pytest.mark.parametrize("mode", [StorageMode.FULL, StorageMode.LOCKED])
def test_full_and_locked_storage_leave_old_zones_intact(harness, mode):
    store_zone(harness, 1, SQUARE)
    before = harness.storage.read_all()
    upload(harness, 2, OTHER_SQUARE)
    harness.storage.mode = mode
    reply = commit(harness, 2, OTHER_SQUARE)
    harness.storage.mode = StorageMode.OK
    assert reply == refused(RejectCode.ZONE_STORAGE_ERROR)
    assert harness.storage.read_all() == before


def test_unreadable_storage_at_commit_is_a_storage_error(harness):
    upload(harness, 2, OTHER_SQUARE)
    harness.storage.mode = StorageMode.UNREADABLE
    assert commit(harness, 2, OTHER_SQUARE) == refused(RejectCode.ZONE_STORAGE_ERROR)
    harness.storage.mode = StorageMode.OK
    assert harness.storage.read_all() == []


# ---- order: when several checks fail only the first is reported ---------------------------------


def test_bad_checksum_is_reported_before_self_crossing(harness):
    upload(harness, 2, BOW_TIE)
    reply = commit(harness, 2, BOW_TIE, crc=zone_crc32(2, BOW_TIE) ^ 1)
    assert reply == refused(RejectCode.ZONE_BAD_CHECKSUM)


def test_degenerate_is_reported_before_self_crossing(harness):
    # A repeated point and a crossing together.
    points = [(0, 0), (1000, 1000), (1000, 1000), (1000, 0), (0, 1000)]
    upload(harness, 2, points)
    assert commit(harness, 2, points) == refused(RejectCode.ZONE_DEGENERATE)


def test_self_crossing_is_reported_before_duplicate_id(harness):
    store_zone(harness, 2, SQUARE)
    upload(harness, 2, BOW_TIE)
    assert commit(harness, 2, BOW_TIE) == refused(RejectCode.ZONE_SELF_CROSSING)


def test_duplicate_id_is_reported_before_store_full(harness):
    for zone_id in range(7):
        harness.storage.write_zone(StoredZone(zone_id, 0, 3, bytes(12)))
    upload(harness, 5, SQUARE)  # id 5 is already stored
    harness.storage.write_zone(StoredZone(30, 0, 3, bytes(12)))
    assert commit(harness, 5, SQUARE) == refused(RejectCode.ZONE_DUPLICATE_ID)


def test_store_full_is_reported_before_storage_error(harness):
    for zone_id in range(7):
        harness.storage.write_zone(StoredZone(zone_id, 0, 3, bytes(12)))
    upload(harness, 20, SQUARE)
    harness.storage.write_zone(StoredZone(30, 0, 3, bytes(12)))
    harness.storage.mode = StorageMode.LOCKED  # reads still work, so the full check runs first
    reply = commit(harness, 20, SQUARE)
    harness.storage.mode = StorageMode.OK
    assert reply == refused(RejectCode.ZONE_STORE_FULL)


# ---- every commit outcome ends the session ------------------------------------------------------


def test_every_outcome_returns_to_disarmed_and_a_new_upload_can_start(harness):
    upload(harness, 1, SQUARE)
    commit(harness, 1, SQUARE)
    assert harness.device.state is State.DISARMED
    upload(harness, 2, BOW_TIE)
    commit(harness, 2, BOW_TIE)
    assert harness.device.state is State.DISARMED
    assert harness.reply(ZoneBegin(3, 3)).result is Result.OK
    assert harness.device.state is State.ZONE_SETUP
