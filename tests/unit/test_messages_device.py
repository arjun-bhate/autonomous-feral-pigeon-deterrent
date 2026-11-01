"""Tests for device-to-host messages (T009)."""

import struct
from dataclasses import fields

import pytest
from hypothesis import given
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
from deterrent.protocol.framing import Frame, parse_frame
from deterrent.protocol.messages import (
    EffectorEventMessage,
    GenericReject,
    RejectNotice,
    Reply,
    StatusBlock,
    StatusEvent,
    ZoneEntry,
    decode_device_message,
    device_frame,
    encode_device_message,
    pack_angles,
    pack_heartbeat_data,
    pack_zone_list,
    unpack_angles,
    unpack_fault_mask,
    unpack_granted_on_ms,
    unpack_heartbeat_data,
    unpack_zone_list,
)

i16 = st.integers(-32768, 32767)
u8 = st.integers(0, 255)
u16 = st.integers(0, 65535)
u32 = st.integers(0, 2**32 - 1)
ACTIVE_DENIAL_CODES = [int(r) for r in DenyReason if r is not DenyReason.RATE_LIMIT]

status_blocks = st.builds(
    StatusBlock,
    state=st.sampled_from(list(State)),
    pan=i16,
    tilt=i16,
    key_on=st.booleans(),
    armed=st.booleans(),
    homed=st.booleans(),
    zone_store_valid=st.booleans(),
    effector_on=st.booleans(),
    fault_mask=u16,
    zone_count=u8,
)

replies = st.builds(
    Reply,
    request_type=st.sampled_from(list(MsgType)),
    result=st.sampled_from(list(Result)),
    code=u8,
    data=st.binary(max_size=57),
).filter(lambda reply: not (reply.result is Result.DENIED and reply.code == 13))

effector_events = st.one_of(
    st.builds(
        EffectorEventMessage,
        event=st.sampled_from(
            [EffectorEvent.ON, EffectorEvent.NORMAL_END, EffectorEvent.STOPPED_BY_HOST]
        ),
        cause=st.just(0),
    ),
    st.builds(
        EffectorEventMessage,
        event=st.just(EffectorEvent.CUT),
        cause=st.sampled_from(ACTIVE_DENIAL_CODES),
    ),
)

DEVICE_MESSAGES = st.one_of(
    replies,
    st.builds(GenericReject, request_type=u8),
    st.builds(StatusEvent, status=status_blocks),
    st.builds(RejectNotice, cause=u8, total_count=u16),
    effector_events,
)

SAMPLE_STATUS = StatusBlock(
    state=State.SCANNING,
    pan=6000,
    tilt=2000,
    key_on=True,
    armed=True,
    homed=True,
    zone_store_valid=True,
    effector_on=False,
    fault_mask=0x0011,
    zone_count=3,
)


# ---- round trips ------------------------------------------------------------------------


@given(DEVICE_MESSAGES)
def test_round_trip_each_reply_and_event(message):
    msg_type, payload = encode_device_message(message)
    assert decode_device_message(msg_type, payload) == message


@given(DEVICE_MESSAGES, st.integers(0, 65535))
def test_round_trip_through_a_real_frame(message, seq):
    frame = parse_frame(device_frame(message, seq)[:-1])
    assert isinstance(frame, Frame)
    assert frame.seq == seq
    assert decode_device_message(frame.type, frame.payload) == message


@pytest.mark.parametrize("request_type", list(MsgType))
def test_reply_type_is_the_request_type_with_bit_7_set(request_type):
    msg_type, payload = encode_device_message(Reply(request_type, Result.OK, 0))
    assert msg_type == request_type | 0x80
    assert payload == bytes([0, 0])


def test_reply_layout_is_result_code_then_data():
    reply = Reply(MsgType.MOVE, Result.OK, 0, pack_angles(6000, -1))
    assert encode_device_message(reply) == (0x82, bytes.fromhex("00007017ffff"))


def test_reply_data_is_limited_to_57_bytes():
    Reply(MsgType.ZONE_LIST, Result.OK, 0, bytes(57)).encode_payload()
    with pytest.raises(ValueError):
        Reply(MsgType.ZONE_LIST, Result.OK, 0, bytes(58)).encode_payload()


def test_reply_to_an_unknown_request_type_cannot_be_built_with_the_normal_reply():
    with pytest.raises(ValueError):
        encode_device_message(Reply(0x7F, Result.OK, 0))


# ---- per-message reply data ------------------------------------------------------------


def test_move_and_jog_reply_data_is_pan_then_tilt():
    assert pack_angles(6000, 2000) == bytes.fromhex("7017d007")
    assert unpack_angles(pack_angles(-5, 8000)) == (-5, 8000)


def test_heartbeat_reply_data_is_device_ms_then_state():
    data = pack_heartbeat_data(0x01020304, State.SCANNING)
    assert data == bytes.fromhex("0403020103")
    assert unpack_heartbeat_data(data) == (0x01020304, State.SCANNING)


def test_granted_reply_carries_granted_on_ms():
    reply = Reply.granted(123456)
    assert (reply.request_type, reply.result, reply.code) == (
        MsgType.FIRE_REQUEST,
        Result.GRANTED,
        0,
    )
    assert unpack_granted_on_ms(reply.data) == 123456
    assert len(reply.data) == 4


def test_denied_reply_carries_the_permanent_code_not_the_priority():
    # PERSON_CLEAR_DELAY has priority 8 but permanent code 14; ELEVATION_LIMIT 11 and 10.
    assert Reply.denied(DenyReason.PERSON_CLEAR_DELAY).code == 14
    assert Reply.denied(DenyReason.REQUEST_EXPIRED).code == 8
    assert Reply.denied(DenyReason.ELEVATION_LIMIT).code == 10
    assert Reply.denied(DenyReason.COOLDOWN_ACTIVE).code == 15
    assert Reply.denied(DenyReason.KEY_OFF).code == 5  # denial code, not the reject code 13
    wire_payload = encode_device_message(Reply.denied(DenyReason.PERSON_CLEAR_DELAY))[1]
    assert wire_payload[:2] == bytes([Result.DENIED, 14])


def test_denied_reply_carries_a_fault_mask_that_is_zero_unless_given():
    assert unpack_fault_mask(Reply.denied(DenyReason.NOT_ARMED).data) == 0
    reply = Reply.denied(DenyReason.FAULT_ACTIVE, fault_mask=0x000C)
    assert unpack_fault_mask(reply.data) == 0x000C
    assert reply.data == bytes.fromhex("0c00")


def test_denied_reply_with_the_retired_code_13_is_refused_at_encode_time():
    with pytest.raises(ValueError):
        encode_device_message(Reply.denied(DenyReason.RATE_LIMIT))
    with pytest.raises(ValueError):
        encode_device_message(Reply(MsgType.FIRE_REQUEST, Result.DENIED, 13, bytes(2)))


def test_rejected_reply_may_use_code_13_because_it_is_a_reject_code():
    reply = Reply.rejected(MsgType.ARM, RejectCode.KEY_OFF)
    assert reply.code == 13
    assert encode_device_message(reply)[1] == bytes([Result.REJECTED, 13])


def test_ok_reply_helper():
    reply = Reply.ok(MsgType.ARM)
    assert (reply.result, reply.code, reply.data) == (Result.OK, 0, b"")


def test_zone_list_entries_are_six_bytes_each_after_a_count():
    entries = [ZoneEntry(1, 4, 0xCBF43926), ZoneEntry(7, 16, 0x01020304)]
    data = pack_zone_list(entries)
    assert data == bytes.fromhex("0201042639f4cb071004030201")
    assert unpack_zone_list(data) == entries


def test_empty_zone_list():
    assert pack_zone_list([]) == b"\x00"
    assert unpack_zone_list(b"\x00") == []


def test_zone_list_for_8_zones_fits_the_59_byte_payload_limit():
    entries = [ZoneEntry(n, 16, 0xFFFFFFFF - n) for n in range(8)]
    reply = Reply(MsgType.ZONE_LIST, Result.OK, 0, pack_zone_list(entries))
    msg_type, payload = encode_device_message(reply)
    assert len(payload) == 2 + 1 + 8 * 6 <= 59
    wire = device_frame(reply, 9)
    frame = parse_frame(wire[:-1])
    assert isinstance(frame, Frame)
    decoded = decode_device_message(frame.type, frame.payload)
    assert unpack_zone_list(decoded.data) == entries


def test_zone_list_nine_zones_still_fit_and_ten_do_not():
    nine = pack_zone_list([ZoneEntry(n, 3, n) for n in range(9)])
    assert len(nine) + 2 <= 59
    with pytest.raises(ValueError):
        pack_zone_list([ZoneEntry(n, 3, n) for n in range(10)])


def test_malformed_reply_data_is_refused_by_the_accessors():
    with pytest.raises(ValueError):
        unpack_angles(b"\x00")
    with pytest.raises(ValueError):
        unpack_zone_list(bytes.fromhex("0201042639f4cb"))
    with pytest.raises(ValueError):
        unpack_granted_on_ms(b"")


# ---- status block -----------------------------------------------------------------------


def test_status_block_is_exactly_13_bytes_in_the_documented_order():
    payload = SAMPLE_STATUS.encode_payload()
    assert len(payload) == 13
    assert payload == bytes.fromhex(
        "03"  # state SCANNING
        "7017"  # pan 6000
        "d007"  # tilt 2000
        "01"  # key_on
        "01"  # armed
        "01"  # homed
        "01"  # zone_store_valid
        "00"  # effector_on
        "1100"  # fault_mask 0x0011
        "03"  # zone_count
    )


def test_status_block_field_order_matches_the_protocol_list():
    names = [
        "state",
        "pan",
        "tilt",
        "key_on",
        "armed",
        "homed",
        "zone_store_valid",
        "effector_on",
        "fault_mask",
        "zone_count",
    ]
    assert [field.name for field in fields(StatusBlock)] == names


def test_status_reply_and_status_event_carry_the_same_block():
    reply = Reply(MsgType.STATUS_REQUEST, Result.OK, 0, SAMPLE_STATUS.encode_payload())
    assert StatusBlock.decode_payload(reply.data) == SAMPLE_STATUS
    msg_type, payload = encode_device_message(StatusEvent(SAMPLE_STATUS))
    assert msg_type == 0xC0
    assert payload == SAMPLE_STATUS.encode_payload()


def test_status_block_with_a_bad_state_or_boolean_is_field_range():
    good = bytearray(SAMPLE_STATUS.encode_payload())
    bad_state = bytes([12]) + bytes(good[1:])
    assert StatusBlock.decode_payload(bad_state) is RejectCode.FIELD_RANGE
    bad_bool = bytearray(good)
    bad_bool[5] = 2
    assert StatusBlock.decode_payload(bytes(bad_bool)) is RejectCode.FIELD_RANGE
    assert StatusBlock.decode_payload(bytes(good[:-1])) is RejectCode.BAD_LENGTH


# ---- events ----------------------------------------------------------------------------


def test_event_types():
    assert encode_device_message(StatusEvent(SAMPLE_STATUS))[0] == EventType.STATUS
    assert encode_device_message(RejectNotice(2, 7))[0] == EventType.REJECT_NOTICE
    assert (
        encode_device_message(EffectorEventMessage(EffectorEvent.ON, 0))[0]
        == EventType.EFFECTOR_EVENT
    )


def test_reject_notice_layout():
    assert encode_device_message(RejectNotice(RejectCode.BAD_CRC, 0x0102)) == (
        0xC1,
        bytes.fromhex("020201"),
    )


def test_effector_event_layout():
    assert encode_device_message(EffectorEventMessage(EffectorEvent.ON, 0)) == (0xC2, b"\x01\x00")
    cut = EffectorEventMessage(EffectorEvent.CUT, int(DenyReason.PERSON_NOT_CLEAR))
    assert encode_device_message(cut) == (0xC2, b"\x03\x07")


def test_effector_event_cause_13_is_refused_at_encode_time():
    with pytest.raises(ValueError):
        encode_device_message(EffectorEventMessage(EffectorEvent.CUT, 13))


@pytest.mark.parametrize("cause", [0, 16, 99, 255])
def test_effector_cut_needs_an_active_denial_code(cause):
    with pytest.raises(ValueError):
        encode_device_message(EffectorEventMessage(EffectorEvent.CUT, cause))


@pytest.mark.parametrize(
    "event", [EffectorEvent.ON, EffectorEvent.NORMAL_END, EffectorEvent.STOPPED_BY_HOST]
)
def test_effector_events_other_than_cut_carry_cause_0(event):
    encode_device_message(EffectorEventMessage(event, 0))
    with pytest.raises(ValueError):
        encode_device_message(EffectorEventMessage(event, 7))


def test_a_received_cut_with_cause_13_can_still_be_decoded_so_a_runner_can_flag_it():
    decoded = decode_device_message(0xC2, bytes([3, 13]))
    assert decoded == EffectorEventMessage(EffectorEvent.CUT, 13)


def test_unknown_effector_event_value_is_field_range():
    assert decode_device_message(0xC2, bytes([9, 0])) is RejectCode.FIELD_RANGE


# ---- generic reject --------------------------------------------------------------------


def test_generic_reject_layout():
    message = GenericReject(0x77)
    assert encode_device_message(message) == (
        0xFE,
        bytes([Result.REJECTED, RejectCode.UNKNOWN_TYPE, 0x77]),
    )


def test_generic_reject_must_say_unknown_type():
    assert decode_device_message(0xFE, bytes([1, 4, 0x77])) == GenericReject(0x77)
    assert decode_device_message(0xFE, bytes([1, 5, 0x77])) is RejectCode.FIELD_RANGE
    assert decode_device_message(0xFE, bytes([0, 4, 0x77])) is RejectCode.FIELD_RANGE


# ---- decoding failures -----------------------------------------------------------------


def test_reply_shorter_than_result_and_code_is_bad_length():
    assert decode_device_message(0x82, b"\x00") is RejectCode.BAD_LENGTH
    assert decode_device_message(0x82, b"") is RejectCode.BAD_LENGTH


def test_reply_with_an_unknown_result_is_field_range():
    assert decode_device_message(0x82, bytes([9, 0])) is RejectCode.FIELD_RANGE


def test_wrong_payload_lengths_for_events_are_bad_length():
    assert decode_device_message(0xC1, b"\x02") is RejectCode.BAD_LENGTH
    assert decode_device_message(0xC2, b"\x01") is RejectCode.BAD_LENGTH
    assert decode_device_message(0xC0, bytes(12)) is RejectCode.BAD_LENGTH
    assert decode_device_message(0xFE, b"\x01\x04") is RejectCode.BAD_LENGTH


@pytest.mark.parametrize("msg_type", [0x00, 0x04, 0x20, 0x7F, 0x87, 0xB1, 0xC3, 0xFD, 0xFF])
def test_unknown_device_types(msg_type):
    assert decode_device_message(msg_type, b"\x00\x00") is RejectCode.UNKNOWN_TYPE


def test_decoded_reply_keeps_the_request_type():
    decoded = decode_device_message(0xA0, bytes([Result.GRANTED, 0]) + struct.pack("<I", 5))
    assert decoded == Reply.granted(5)
