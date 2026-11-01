"""Tests for the zone upload session: ZONE_BEGIN, ZONE_POINT, idle timeout (T021)."""

import pytest

from deterrent.protocol.codes import MsgType, RejectCode, Result, State
from deterrent.protocol.messages import (
    Arm,
    Disarm,
    Heartbeat,
    Move,
    Reply,
    StatusRequest,
    ZoneBegin,
    ZoneList,
    ZonePoint,
)
from deterrent.simulator.zone_storage import StoredZone
from support.device_harness import Harness

TIMEOUT = 5000


def begin_refused(code: RejectCode) -> Reply:
    return Reply.rejected(MsgType.ZONE_BEGIN, code)


def point_refused(code: RejectCode) -> Reply:
    return Reply.rejected(MsgType.ZONE_POINT, code)


def ok(msg_type: MsgType) -> Reply:
    return Reply.ok(msg_type)


@pytest.fixture
def harness() -> Harness:
    return Harness.homed()


def test_begin_from_disarmed_enters_zone_setup(harness):
    assert harness.reply(ZoneBegin(1, 4)) == ok(MsgType.ZONE_BEGIN)
    assert harness.device.state is State.ZONE_SETUP
    assert harness.device.zones.upload_summary() == (1, 4, 0)


def test_begin_while_scanning_is_wrong_state(harness):
    harness.control.set_key(True)
    harness.reply(Arm())
    assert harness.device.state is State.SCANNING
    assert harness.reply(ZoneBegin(1, 4)) == begin_refused(RejectCode.WRONG_STATE)
    assert harness.device.state is State.SCANNING
    assert harness.device.zones.upload_summary() is None


def test_begin_before_homing_is_refused():
    boot = Harness(boot_duration_ms=500)
    assert boot.reply(ZoneBegin(1, 4)) == begin_refused(RejectCode.NOT_HOMED)
    homing = Harness()
    homing.advance(500)
    assert homing.reply(ZoneBegin(1, 4)) == begin_refused(RejectCode.BUSY_HOMING)
    assert homing.device.state is State.HOMING


@pytest.mark.parametrize(
    ("count", "code"),
    [(0, RejectCode.ZONE_TOO_FEW_POINTS), (2, RejectCode.ZONE_TOO_FEW_POINTS)]
    + [(17, RejectCode.ZONE_TOO_MANY_POINTS), (255, RejectCode.ZONE_TOO_MANY_POINTS)],
)
def test_wrong_point_counts_are_rejected_with_the_specific_code_and_state_unchanged(
    harness, count, code
):
    assert harness.reply(ZoneBegin(1, count)) == begin_refused(code)
    assert harness.device.state is State.DISARMED
    assert harness.device.zones.upload_summary() is None


@pytest.mark.parametrize("count", [3, 16])
def test_the_limits_themselves_are_accepted(harness, count):
    assert harness.reply(ZoneBegin(1, count)).result is Result.OK


def test_begin_with_a_full_store_is_rejected(harness):
    for zone_id in range(8):
        harness.storage.write_zone(StoredZone(zone_id, 0, 3, bytes(12)))
    assert harness.reply(ZoneBegin(20, 4)) == begin_refused(RejectCode.ZONE_STORE_FULL)
    assert harness.device.state is State.DISARMED


def test_a_second_begin_replaces_the_first_and_nothing_is_mixed(harness):
    harness.reply(ZoneBegin(1, 3))
    harness.reply(ZonePoint(0, 10, 10))
    harness.reply(ZonePoint(1, 20, 20))
    assert harness.reply(ZoneBegin(2, 4)) == ok(MsgType.ZONE_BEGIN)
    assert harness.device.state is State.ZONE_SETUP
    assert harness.device.zones.upload_summary() == (2, 4, 0)
    assert harness.reply(ZonePoint(0, 5, 5)) == ok(MsgType.ZONE_POINT)
    assert harness.device.zones.upload_points() == [(5, 5)]


def test_a_rejected_begin_leaves_an_upload_in_progress_untouched(harness):
    harness.reply(ZoneBegin(1, 3))
    harness.reply(ZonePoint(0, 10, 10))
    assert harness.reply(ZoneBegin(9, 2)) == begin_refused(RejectCode.ZONE_TOO_FEW_POINTS)
    assert harness.device.state is State.ZONE_SETUP
    assert harness.device.zones.upload_summary() == (1, 3, 1)
    assert harness.device.zones.upload_points() == [(10, 10)]


def test_points_must_arrive_in_order_and_a_bad_index_keeps_the_upload(harness):
    harness.reply(ZoneBegin(1, 4))
    assert harness.reply(ZonePoint(1, 1, 1)) == point_refused(RejectCode.ZONE_BAD_INDEX)
    assert harness.reply(ZonePoint(0, 1, 1)) == ok(MsgType.ZONE_POINT)
    assert harness.reply(ZonePoint(0, 1, 1)) == point_refused(RejectCode.ZONE_BAD_INDEX)
    assert harness.reply(ZonePoint(2, 3, 3)) == point_refused(RejectCode.ZONE_BAD_INDEX)
    assert harness.reply(ZonePoint(1, 2, 2)) == ok(MsgType.ZONE_POINT)
    assert harness.device.state is State.ZONE_SETUP
    assert harness.device.zones.upload_points() == [(1, 1), (2, 2)]


def test_a_point_beyond_the_declared_count_is_a_bad_index(harness):
    harness.reply(ZoneBegin(1, 3))
    for index in range(3):
        harness.reply(ZonePoint(index, index, index))
    assert harness.reply(ZonePoint(3, 3, 3)) == point_refused(RejectCode.ZONE_BAD_INDEX)
    assert harness.device.zones.upload_summary() == (1, 3, 3)


def test_a_point_with_no_upload_is_zone_no_upload(harness):
    assert harness.reply(ZonePoint(0, 1, 1)) == point_refused(RejectCode.ZONE_NO_UPLOAD)
    assert harness.device.state is State.DISARMED


def test_the_staging_buffer_is_never_persisted(harness):
    harness.reply(ZoneBegin(1, 3))
    harness.reply(ZonePoint(0, 1, 1))
    assert harness.storage.read_all() == []


# ---- the idle timeout -------------------------------------------------------------------


def test_timeout_one_ms_before_the_limit_is_still_zone_setup_and_at_the_limit_disarmed(harness):
    harness.reply(ZoneBegin(1, 3))
    harness.advance(TIMEOUT - 1)
    assert harness.device.state is State.ZONE_SETUP
    harness.advance(1)
    assert harness.device.state is State.DISARMED
    assert harness.device.zones.upload_summary() is None


def test_a_point_after_the_timeout_gets_zone_no_upload(harness):
    harness.reply(ZoneBegin(1, 3))
    harness.advance(TIMEOUT)
    assert harness.reply(ZonePoint(0, 1, 1)) == point_refused(RejectCode.ZONE_NO_UPLOAD)


def test_every_accepted_upload_message_resets_the_timer(harness):
    harness.reply(ZoneBegin(1, 4))
    harness.advance(TIMEOUT - 1)
    harness.reply(ZonePoint(0, 1, 1))  # resets
    harness.advance(TIMEOUT - 1)
    assert harness.device.state is State.ZONE_SETUP
    harness.reply(ZonePoint(1, 2, 2))  # resets again
    harness.advance(TIMEOUT - 1)
    assert harness.device.state is State.ZONE_SETUP
    harness.advance(1)
    assert harness.device.state is State.DISARMED


def test_heartbeats_list_move_status_and_stale_messages_do_not_reset_the_timer(harness):
    harness.reply(ZoneBegin(1, 4))
    begin_time = harness.clock.now_ms()
    stale_seq = harness.seq - 50
    while harness.clock.now_ms() - begin_time < TIMEOUT - 200:
        harness.advance(200)
        harness.reply(Heartbeat(True, 0))
        harness.send(Move(10, 10))
        harness.send(ZoneList())
        harness.send(StatusRequest())
        harness.send(Heartbeat(True, 0), seq=stale_seq & 0xFFFF)
    assert harness.device.state is State.ZONE_SETUP
    harness.advance(begin_time + TIMEOUT - harness.clock.now_ms())
    assert harness.device.state is State.DISARMED


def test_rejected_upload_messages_do_not_reset_the_timer(harness):
    harness.reply(ZoneBegin(1, 4))
    harness.advance(TIMEOUT - 100)
    assert harness.reply(ZonePoint(3, 1, 1)) == point_refused(RejectCode.ZONE_BAD_INDEX)
    assert harness.reply(ZoneBegin(9, 2)) == begin_refused(RejectCode.ZONE_TOO_FEW_POINTS)
    harness.advance(100)
    assert harness.device.state is State.DISARMED


def test_a_new_upload_can_start_after_a_timeout(harness):
    harness.reply(ZoneBegin(1, 3))
    harness.advance(TIMEOUT)
    assert harness.reply(ZoneBegin(1, 3)).result is Result.OK
    assert harness.device.state is State.ZONE_SETUP


def test_disarm_discards_the_upload_and_leaves_zone_setup(harness):
    harness.reply(ZoneBegin(1, 3))
    harness.reply(ZonePoint(0, 1, 1))
    harness.reply(Disarm())
    assert harness.device.state is State.DISARMED
    assert harness.device.zones.upload_summary() is None
