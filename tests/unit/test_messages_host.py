"""Tests for host-to-device messages (T008)."""

import struct

import pytest
from hypothesis import given
from hypothesis import strategies as st

from deterrent.protocol.codes import MsgType, RejectCode
from deterrent.protocol.framing import Frame, build_frame, parse_frame
from deterrent.protocol.messages import (
    Arm,
    Disarm,
    FireRequest,
    FireStop,
    Heartbeat,
    Home,
    Jog,
    Move,
    StatusRequest,
    ZoneBegin,
    ZoneClear,
    ZoneCommit,
    ZoneDelete,
    ZoneList,
    ZonePoint,
    decode_host_message,
    encode_host_message,
    host_frame,
)

i16 = st.integers(-32768, 32767)
u8 = st.integers(0, 255)
u16 = st.integers(0, 65535)
u32 = st.integers(0, 2**32 - 1)

MESSAGES = st.one_of(
    st.just(Home()),
    st.builds(Move, pan=i16, tilt=i16),
    st.builds(Jog, dpan=i16, dtilt=i16),
    st.builds(Heartbeat, person_clear=st.booleans(), laptop_ts_ms=u32),
    st.just(Arm()),
    st.just(Disarm()),
    st.builds(ZoneBegin, zone_id=u8, point_count=u8),
    st.builds(ZonePoint, index=u8, pan=i16, tilt=i16),
    st.builds(ZoneCommit, crc32=u32),
    st.builds(ZoneDelete, zone_id=u8),
    st.just(ZoneList()),
    st.just(ZoneClear()),
    st.builds(
        FireRequest,
        issued_at_ms=u32,
        ttl_ms=st.integers(1, 5000),
        intended_pan=i16,
        intended_tilt=i16,
        duration_ms=st.integers(500, 65535),
    ),
    st.just(FireStop()),
    st.just(StatusRequest()),
)

# Expected payload length for every host message type.
PAYLOAD_LENGTHS = {
    MsgType.HOME: 0,
    MsgType.MOVE: 4,
    MsgType.JOG: 4,
    MsgType.HEARTBEAT: 5,
    MsgType.ARM: 0,
    MsgType.DISARM: 0,
    MsgType.ZONE_BEGIN: 2,
    MsgType.ZONE_POINT: 5,
    MsgType.ZONE_COMMIT: 4,
    MsgType.ZONE_DELETE: 1,
    MsgType.ZONE_LIST: 0,
    MsgType.ZONE_CLEAR: 0,
    MsgType.FIRE_REQUEST: 12,
    MsgType.FIRE_STOP: 0,
    MsgType.STATUS_REQUEST: 0,
}

# A valid payload for every type, used to build "valid except for one thing" cases.
VALID_PAYLOADS = {
    MsgType.HOME: b"",
    MsgType.MOVE: struct.pack("<hh", 6000, 2000),
    MsgType.JOG: struct.pack("<hh", -100, 50),
    MsgType.HEARTBEAT: struct.pack("<BI", 1, 123456),
    MsgType.ARM: b"",
    MsgType.DISARM: b"",
    MsgType.ZONE_BEGIN: struct.pack("<BB", 1, 4),
    MsgType.ZONE_POINT: struct.pack("<Bhh", 0, 100, 200),
    MsgType.ZONE_COMMIT: struct.pack("<I", 0xDEADBEEF),
    MsgType.ZONE_DELETE: struct.pack("<B", 1),
    MsgType.ZONE_LIST: b"",
    MsgType.ZONE_CLEAR: b"",
    MsgType.FIRE_REQUEST: struct.pack("<IHhhH", 1000, 500, 6000, 2000, 1500),
    MsgType.FIRE_STOP: b"",
    MsgType.STATUS_REQUEST: b"",
}


def fire_payload(ttl=500, duration=1500):
    return struct.pack("<IHhhH", 1000, ttl, 6000, 2000, duration)


# ---- round trips and layout ------------------------------------------------------------


@given(MESSAGES)
def test_round_trip_each_type(message):
    msg_type, payload = encode_host_message(message)
    assert decode_host_message(msg_type, payload) == message


@given(MESSAGES, st.integers(0, 65535))
def test_round_trip_through_a_real_frame(message, seq):
    wire = host_frame(message, seq)
    frame = parse_frame(wire[:-1])
    assert isinstance(frame, Frame)
    assert frame.seq == seq
    assert decode_host_message(frame.type, frame.payload) == message


def test_every_message_type_is_covered_by_a_class():
    seen = {encode_host_message(message)[0] for message in VALID_PAYLOADS_MESSAGES}
    assert seen == set(MsgType)


VALID_PAYLOADS_MESSAGES = [
    Home(),
    Move(1, 2),
    Jog(1, 2),
    Heartbeat(True, 1),
    Arm(),
    Disarm(),
    ZoneBegin(1, 3),
    ZonePoint(0, 1, 2),
    ZoneCommit(1),
    ZoneDelete(1),
    ZoneList(),
    ZoneClear(),
    FireRequest(1, 500, 1, 2, 500),
    FireStop(),
    StatusRequest(),
]


@pytest.mark.parametrize("msg_type", list(MsgType))
def test_valid_payload_decodes_and_has_the_documented_length(msg_type):
    assert len(VALID_PAYLOADS[msg_type]) == PAYLOAD_LENGTHS[msg_type]
    assert not isinstance(decode_host_message(msg_type, VALID_PAYLOADS[msg_type]), RejectCode)


def test_exact_byte_layouts_are_little_endian():
    assert encode_host_message(Move(6000, -2)) == (0x02, bytes.fromhex("7017feff"))
    assert encode_host_message(Jog(-1, 1)) == (0x03, bytes.fromhex("ffff0100"))
    assert encode_host_message(Heartbeat(True, 0x01020304)) == (0x04, bytes.fromhex("0104030201"))
    assert encode_host_message(Heartbeat(False, 0)) == (0x04, bytes.fromhex("0000000000"))
    assert encode_host_message(ZoneBegin(7, 4)) == (0x10, bytes([7, 4]))
    assert encode_host_message(ZonePoint(2, 100, -200)) == (0x11, bytes.fromhex("02640038ff"))
    assert encode_host_message(ZoneCommit(0xCBF43926)) == (0x12, bytes.fromhex("2639f4cb"))
    assert encode_host_message(ZoneDelete(9)) == (0x13, bytes([9]))
    assert encode_host_message(FireRequest(0x0A0B0C0D, 500, 6000, -1, 1500)) == (
        0x20,
        bytes.fromhex("0d0c0b0af4017017ffffdc05"),
    )


@pytest.mark.parametrize(
    ("message", "msg_type"),
    [
        (Home(), 0x01),
        (Arm(), 0x05),
        (Disarm(), 0x06),
        (ZoneList(), 0x14),
        (ZoneClear(), 0x15),
        (FireStop(), 0x21),
        (StatusRequest(), 0x30),
    ],
)
def test_messages_without_payload_encode_to_nothing(message, msg_type):
    assert encode_host_message(message) == (msg_type, b"")


def test_host_frame_is_a_complete_wire_frame():
    wire = host_frame(Arm(), 5)
    assert wire == build_frame(0x05, 5, b"")


@pytest.mark.parametrize(
    "message",
    [Move(40000, 0), Heartbeat(True, 2**32), ZoneBegin(256, 3), FireRequest(1, 70000, 0, 0, 500)],
)
def test_encoder_refuses_values_that_do_not_fit_their_field(message):
    with pytest.raises(ValueError):
        encode_host_message(message)


# ---- BAD_LENGTH ------------------------------------------------------------------------


@pytest.mark.parametrize("msg_type", list(MsgType))
def test_wrong_length_gives_bad_length_for_every_type(msg_type):
    right = PAYLOAD_LENGTHS[msg_type]
    wrong_lengths = {right + 1, right + 2, 59}
    if right > 0:
        wrong_lengths |= {right - 1, 0}
    for length in sorted(wrong_lengths - {right}):
        result = decode_host_message(msg_type, bytes(length))
        assert result is RejectCode.BAD_LENGTH, (msg_type, length)


def test_fire_stop_with_one_byte_is_bad_length():
    assert decode_host_message(MsgType.FIRE_STOP, b"\x00") is RejectCode.BAD_LENGTH


def test_length_is_checked_before_field_ranges():
    # Heartbeat with person_clear 2 and a missing byte: BAD_LENGTH wins over FIELD_RANGE.
    assert decode_host_message(MsgType.HEARTBEAT, b"\x02\x00\x00\x00") is RejectCode.BAD_LENGTH


# ---- FIELD_RANGE -----------------------------------------------------------------------


@pytest.mark.parametrize("value", [2, 3, 255])
def test_boolean_other_than_0_or_1_is_field_range(value):
    payload = struct.pack("<BI", value, 0)
    assert decode_host_message(MsgType.HEARTBEAT, payload) is RejectCode.FIELD_RANGE


@pytest.mark.parametrize(("value", "expected"), [(0, False), (1, True)])
def test_boolean_0_and_1_are_accepted(value, expected):
    result = decode_host_message(MsgType.HEARTBEAT, struct.pack("<BI", value, 0))
    assert result == Heartbeat(expected, 0)


@pytest.mark.parametrize("duration", [0, 1, 499])
def test_duration_below_500_including_zero_is_field_range(duration):
    result = decode_host_message(MsgType.FIRE_REQUEST, fire_payload(duration=duration))
    assert result is RejectCode.FIELD_RANGE


@pytest.mark.parametrize("duration", [500, 750, 5000, 5001, 65535])
def test_duration_of_500_or_more_passes_the_range_check(duration):
    # 5001 and above are denied later by arbitration (ON_TIME_LIMIT), not rejected here.
    result = decode_host_message(MsgType.FIRE_REQUEST, fire_payload(duration=duration))
    assert result == FireRequest(1000, 500, 6000, 2000, duration)


@pytest.mark.parametrize("ttl", [0, 5001, 65535])
def test_ttl_outside_1_to_5000_is_field_range(ttl):
    result = decode_host_message(MsgType.FIRE_REQUEST, fire_payload(ttl=ttl))
    assert result is RejectCode.FIELD_RANGE


@pytest.mark.parametrize("ttl", [1, 500, 5000])
def test_ttl_inside_1_to_5000_passes(ttl):
    result = decode_host_message(MsgType.FIRE_REQUEST, fire_payload(ttl=ttl))
    assert isinstance(result, FireRequest)
    assert result.ttl_ms == ttl


def test_bad_ttl_and_bad_duration_together_are_still_field_range():
    result = decode_host_message(MsgType.FIRE_REQUEST, fire_payload(ttl=0, duration=0))
    assert result is RejectCode.FIELD_RANGE


def test_angles_are_not_range_checked_here():
    # Soft limits are a device-state decision (OUT_OF_SOFT_LIMIT), not a field range.
    assert decode_host_message(MsgType.MOVE, struct.pack("<hh", -32768, 32767)) == Move(
        -32768, 32767
    )


# ---- UNKNOWN_TYPE ----------------------------------------------------------------------


@pytest.mark.parametrize("msg_type", [0x00, 0x07, 0x16, 0x22, 0x31, 0x82, 0xC0, 0xFE, 0xFF])
def test_unknown_type_is_unknown_type_whatever_the_payload(msg_type):
    for payload in (b"", b"\x01\x02\x03"):
        assert decode_host_message(msg_type, payload) is RejectCode.UNKNOWN_TYPE


def test_unknown_type_is_reported_before_length():
    assert decode_host_message(0x99, bytes(50)) is RejectCode.UNKNOWN_TYPE
