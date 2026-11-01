"""Tests for the device intake: receiver steps 1 to 8 of protocol.md section 2 (T015a, T015b)."""

import struct

import pytest

from deterrent.protocol.codes import MsgType, RejectCode, Result
from deterrent.protocol.crc import crc16_ccitt_false
from deterrent.protocol.framing import build_frame, cobs_encode
from deterrent.protocol.messages import (
    GenericReject,
    Heartbeat,
    Home,
    Move,
    Reply,
    host_frame,
)
from support.device_harness import Harness


def raw_frame(msg_type: int, seq: int, payload: bytes) -> bytes:
    head = bytes([msg_type, seq & 0xFF, seq >> 8]) + payload
    crc = crc16_ccitt_false(head)
    return head + bytes([crc & 0xFF, crc >> 8])


def wire(raw: bytes) -> bytes:
    return cobs_encode(raw) + b"\x00"


def bad_crc_wire(seq: int = 1) -> bytes:
    raw = bytearray(raw_frame(int(MsgType.STATUS_REQUEST), seq, b""))
    raw[-1] ^= 0xFF
    return wire(bytes(raw))


def truncated_wire() -> bytes:
    return wire(b"\x30\x01\x00\x00")


def bad_framing_wire() -> bytes:
    return bytes([0x41]) * 70 + b"\x00"


@pytest.fixture
def harness() -> Harness:
    # A long boot keeps the device in BOOT, so these tests see only the intake rules.
    return Harness(boot_duration_ms=10_000)


# ---- steps 1 to 3: no reply, no state change, counted by cause --------------------------


@pytest.mark.parametrize(
    ("frame", "cause"),
    [
        (bad_crc_wire(), RejectCode.BAD_CRC),
        (truncated_wire(), RejectCode.TRUNCATED),
        (bad_framing_wire(), RejectCode.BAD_FRAMING),
    ],
)
def test_damaged_frames_get_no_reply_and_change_nothing(harness, frame, cause):
    before = harness.snapshot()
    replies = [o for o in harness.send_wire(frame) if not 0xC0 <= o.type <= 0xC2]
    assert replies == []
    assert harness.snapshot() == before
    assert harness.device.reject_counts[cause] == 1


def test_each_rejection_at_steps_1_to_3_is_counted_by_cause(harness):
    for _ in range(3):
        harness.send_wire(bad_crc_wire())
    harness.send_wire(truncated_wire())
    harness.send_wire(bad_framing_wire())
    counts = harness.device.reject_counts
    assert counts[RejectCode.BAD_CRC] == 3
    assert counts[RejectCode.TRUNCATED] == 1
    assert counts[RejectCode.BAD_FRAMING] == 1


def test_the_event_sink_hears_about_every_rejection(harness):
    harness.send_wire(bad_crc_wire())
    harness.send(Move(99999 % 32768, 0), seq=5)  # fine: a valid frame, rejected by state
    causes = [e.details["cause"] for e in harness.sink.named("message_rejected")]
    assert RejectCode.BAD_CRC in causes


# ---- step 5: unknown type ------------------------------------------------------------------


def test_unknown_type_gets_the_generic_reject_and_changes_nothing(harness):
    before = harness.snapshot()
    (out,) = harness.send_raw(0x77, b"\x01\x02", seq=9)
    assert out.type == 0xFE
    assert out.seq == 9
    assert out.message == GenericReject(0x77)
    assert harness.snapshot() == before


def test_a_reply_type_sent_to_the_device_is_unknown_to_it(harness):
    (out,) = harness.send_raw(0x82, b"\x00\x00")
    assert out.message == GenericReject(0x82)


# ---- step 6: length -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("msg_type", "payload"),
    [
        (MsgType.HEARTBEAT, b"\x01\x00\x00\x00"),
        (MsgType.MOVE, b"\x00"),
        (MsgType.ARM, b"\x00"),
        (MsgType.FIRE_STOP, b"\x00"),
        (MsgType.FIRE_REQUEST, bytes(11)),
        (MsgType.ZONE_COMMIT, bytes(5)),
    ],
)
def test_wrong_payload_length_is_rejected_bad_length(harness, msg_type, payload):
    before = harness.snapshot()
    (out,) = harness.send_raw(msg_type, payload)
    assert out.type == (int(msg_type) | 0x80)
    assert out.message == Reply.rejected(msg_type, RejectCode.BAD_LENGTH)
    assert out.message.result is Result.REJECTED
    assert harness.snapshot() == before


# ---- step 7: field ranges ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("msg_type", "payload"),
    [
        (MsgType.HEARTBEAT, struct.pack("<BI", 2, 0)),
        (MsgType.FIRE_REQUEST, struct.pack("<IHhhH", 0, 500, 0, 0, 0)),
        (MsgType.FIRE_REQUEST, struct.pack("<IHhhH", 0, 0, 0, 0, 500)),
        (MsgType.FIRE_REQUEST, struct.pack("<IHhhH", 0, 5001, 0, 0, 500)),
    ],
)
def test_out_of_range_field_is_rejected_field_range(harness, msg_type, payload):
    before = harness.snapshot()
    (out,) = harness.send_raw(msg_type, payload)
    assert out.message == Reply.rejected(msg_type, RejectCode.FIELD_RANGE)
    assert harness.snapshot() == before


def test_rejections_at_steps_5_to_7_are_reported_to_the_sink(harness):
    harness.send_raw(0x77)
    harness.send_raw(MsgType.MOVE, b"\x00")
    harness.send_raw(MsgType.HEARTBEAT, struct.pack("<BI", 3, 0))
    causes = [e.details["cause"] for e in harness.sink.named("message_rejected")]
    assert causes == [RejectCode.UNKNOWN_TYPE, RejectCode.BAD_LENGTH, RejectCode.FIELD_RANGE]


def test_order_of_checks_unknown_then_length_then_range(harness):
    # Unknown type with a payload of any length is UNKNOWN_TYPE, not BAD_LENGTH.
    (out,) = harness.send_raw(0x99, bytes(50))
    assert isinstance(out.message, GenericReject)


# ---- a valid message with no handler is refused, never silently accepted -------------------


def test_a_valid_message_without_a_handler_is_refused_wrong_state(harness):
    harness.device.handlers.clear()
    (out,) = harness.send(Home())
    assert out.message == Reply.rejected(MsgType.HOME, RejectCode.WRONG_STATE)


def test_reply_echoes_the_request_sequence_number(harness):
    harness.device.handlers.clear()
    (out,) = harness.send(Heartbeat(True, 0), seq=1234)
    assert out.seq == 1234


def test_a_frame_split_across_two_deliveries_is_still_one_message(harness):
    harness.device.handlers.clear()
    data = host_frame(Home(), 7)
    assert harness.send_wire(data[:5]) == []
    (out,) = harness.send_wire(data[5:])
    assert out.seq == 7


def test_build_frame_is_what_the_harness_sends():
    assert host_frame(Home(), 3) == build_frame(0x01, 3, b"")


# ---- T015b: the sequence rule (step 4) and the REJECT_NOTICE rate limit ---------------------


class CountingHandler:
    """A stand-in command handler that counts how many times the device really acted."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, message):
        self.calls += 1
        return Reply.ok(message.TYPE)


def with_counter(harness: Harness) -> CountingHandler:
    counter = CountingHandler()
    harness.device.handlers[Heartbeat] = counter
    return counter


def heartbeat_wire(seq: int) -> bytes:
    return host_frame(Heartbeat(True, 0), seq)


@pytest.mark.parametrize("seq", [0, 1, 40000, 65535])
def test_the_first_message_after_boot_is_accepted_whatever_its_number(seq):
    fresh = Harness(boot_duration_ms=10_000)
    counter = with_counter(fresh)
    (out,) = fresh.send_wire(heartbeat_wire(seq))
    assert out.message.result is Result.OK
    assert counter.calls == 1


def test_a_duplicate_gets_identical_reply_bytes_and_exactly_one_action(harness):
    counter = with_counter(harness)
    (first,) = harness.send_wire(heartbeat_wire(10))
    (second,) = harness.send_wire(heartbeat_wire(10))
    assert second.raw == first.raw
    assert counter.calls == 1


def test_an_older_sequence_is_rejected_stale_and_not_acted_on(harness):
    counter = with_counter(harness)
    harness.send_wire(heartbeat_wire(10))
    (out,) = harness.send_wire(heartbeat_wire(5))
    assert out.message == Reply.rejected(MsgType.HEARTBEAT, RejectCode.STALE_SEQ)
    assert out.seq == 5
    assert counter.calls == 1


def test_the_cache_holds_eight_replies(harness):
    counter = with_counter(harness)
    for seq in range(1, 11):
        harness.send_wire(heartbeat_wire(seq))
    assert counter.calls == 10
    # 3 to 10 are cached: replayed, no action.
    harness.send_wire(heartbeat_wire(3))
    harness.send_wire(heartbeat_wire(10))
    assert counter.calls == 10
    # 1 and 2 have been pushed out of the cache and are now simply old.
    for seq in (1, 2):
        (out,) = harness.send_wire(heartbeat_wire(seq))
        assert out.message == Reply.rejected(MsgType.HEARTBEAT, RejectCode.STALE_SEQ)
    assert counter.calls == 10


def test_sequence_wrap_from_65535_to_0_is_accepted(harness):
    counter = with_counter(harness)
    harness.send_wire(heartbeat_wire(65535))
    (out,) = harness.send_wire(heartbeat_wire(0))
    assert out.message.result is Result.OK
    assert counter.calls == 2


def test_a_gap_in_the_numbers_is_accepted(harness):
    counter = with_counter(harness)
    harness.send_wire(heartbeat_wire(1))
    (out,) = harness.send_wire(heartbeat_wire(500))
    assert out.message.result is Result.OK
    assert counter.calls == 2


def test_the_far_side_of_the_circle_is_stale(harness):
    counter = with_counter(harness)
    harness.send_wire(heartbeat_wire(100))
    (out,) = harness.send_wire(heartbeat_wire(100 + 32768))  # distance 32768: not "forward"
    assert out.message == Reply.rejected(MsgType.HEARTBEAT, RejectCode.STALE_SEQ)
    (out,) = harness.send_wire(heartbeat_wire(100 + 32767))  # distance 32767: forward
    assert out.message.result is Result.OK
    assert counter.calls == 2


def test_a_message_rejected_at_steps_5_to_8_still_consumes_its_sequence_number(harness):
    harness.send_raw(0x77, seq=5)  # step 5 failure
    # The same number again is a duplicate: the cached rejection is replayed.
    (replay,) = harness.send_raw(0x77, seq=5)
    assert isinstance(replay.message, GenericReject)
    # A valid message with that number is not processed either.
    counter = with_counter(harness)
    (again,) = harness.send_wire(heartbeat_wire(5))
    assert counter.calls == 0
    assert isinstance(again.message, GenericReject)
    # And a lower number is now stale.
    (old,) = harness.send_wire(heartbeat_wire(4))
    assert old.message == Reply.rejected(MsgType.HEARTBEAT, RejectCode.STALE_SEQ)


def test_a_length_rejection_consumes_its_number_and_is_replayed_unchanged(harness):
    counter = with_counter(harness)
    (first,) = harness.send_raw(MsgType.HEARTBEAT, b"\x01", seq=8)
    (second,) = harness.send_wire(heartbeat_wire(8))
    assert second.raw == first.raw
    assert counter.calls == 0


def test_a_stale_message_with_an_unknown_type_gets_the_generic_reject(harness):
    harness.send_wire(heartbeat_wire(50))
    (out,) = harness.send_raw(0x77, seq=3)
    assert isinstance(out.message, GenericReject)


def notices(harness: Harness):
    return harness.events_of(0xC1)


def test_reject_notice_is_capped_at_five_per_second(harness):
    for _ in range(10):
        harness.send_wire(bad_crc_wire())
    assert len(notices(harness)) == 5
    harness.clock.advance(999)
    harness.send_wire(bad_crc_wire())
    assert len(notices(harness)) == 5
    harness.clock.advance(1)  # exactly one second after the first five
    harness.send_wire(bad_crc_wire())
    assert len(notices(harness)) == 6


def test_reject_notice_carries_the_cause_and_the_running_total(harness):
    harness.send_wire(bad_crc_wire())
    harness.send_wire(truncated_wire())
    first, second = [o.message for o in notices(harness)]
    assert (first.cause, first.total_count) == (RejectCode.BAD_CRC, 1)
    assert (second.cause, second.total_count) == (RejectCode.TRUNCATED, 2)


def test_every_rejection_is_still_counted_when_the_notice_is_suppressed(harness):
    for _ in range(10):
        harness.send_wire(bad_crc_wire())
    assert harness.device.reject_counts[RejectCode.BAD_CRC] == 10


def test_stale_and_duplicate_messages_are_not_counted_as_frame_failures(harness):
    with_counter(harness)
    harness.send_wire(heartbeat_wire(10))
    harness.send_wire(heartbeat_wire(10))
    harness.send_wire(heartbeat_wire(2))
    assert harness.device.reject_counts == {}
    assert notices(harness) == []
