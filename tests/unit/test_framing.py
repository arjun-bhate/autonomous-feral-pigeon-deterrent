"""Tests for COBS and frame building/parsing (T006)."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from deterrent.protocol.crc import crc16_ccitt_false
from deterrent.protocol.framing import (
    MAX_BODY_BYTES,
    MAX_PAYLOAD_BYTES,
    MAX_RAW_FRAME_BYTES,
    MAX_WIRE_BYTES,
    MIN_RAW_FRAME_BYTES,
    CobsError,
    Frame,
    FrameError,
    FrameReader,
    build_frame,
    cobs_decode,
    cobs_encode,
    parse_frame,
)

payloads = st.binary(min_size=0, max_size=MAX_PAYLOAD_BYTES)
types = st.integers(0, 255)
seqs = st.integers(0, 65535)


def raw_frame(msg_type: int, seq: int, payload: bytes) -> bytes:
    """A raw frame built by hand, independent of build_frame."""
    body = bytes([msg_type, seq & 0xFF, seq >> 8]) + payload
    crc = crc16_ccitt_false(body)
    return body + bytes([crc & 0xFF, crc >> 8])


def body_of(wire: bytes) -> bytes:
    assert wire[-1] == 0
    return wire[:-1]


# ---- COBS -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("plain", "encoded"),
    [
        (b"", b"\x01"),
        (b"\x00", b"\x01\x01"),
        (b"\x00\x00", b"\x01\x01\x01"),
        (b"\x11\x22\x00\x33", b"\x03\x11\x22\x02\x33"),
        (b"\x11\x22\x33\x44", b"\x05\x11\x22\x33\x44"),
        (b"\x11\x00\x00\x00", b"\x02\x11\x01\x01\x01"),
    ],
)
def test_cobs_known_vectors(plain, encoded):
    assert cobs_encode(plain) == encoded
    assert cobs_decode(encoded) == plain


@given(st.binary(min_size=0, max_size=700))
def test_cobs_round_trip_any_length(data):
    encoded = cobs_encode(data)
    assert 0 not in encoded
    assert cobs_decode(encoded) == data


def test_cobs_long_runs_use_the_ff_block():
    data = bytes([7]) * 254
    assert cobs_encode(data)[0] == 0xFF
    assert cobs_decode(cobs_encode(data)) == data


@pytest.mark.parametrize("bad", [b"\x00", b"\x05\x11\x22", b"\x03\x11\x00\x33", b"\x02"])
def test_cobs_decode_refuses_invalid_input(bad):
    with pytest.raises(CobsError):
        cobs_decode(bad)


# ---- build_frame ------------------------------------------------------------------------


def test_constants_match_the_protocol():
    assert (MIN_RAW_FRAME_BYTES, MAX_RAW_FRAME_BYTES, MAX_PAYLOAD_BYTES) == (5, 64, 59)
    assert (MAX_BODY_BYTES, MAX_WIRE_BYTES) == (65, 66)


def test_build_frame_layout_matches_a_hand_built_frame():
    wire = build_frame(0x04, 0x0102, b"\x01\x10\x20\x30\x40")
    assert wire[-1] == 0
    assert cobs_decode(body_of(wire)) == raw_frame(0x04, 0x0102, b"\x01\x10\x20\x30\x40")


def test_crc_is_appended_low_byte_first():
    raw = cobs_decode(body_of(build_frame(0x30, 7, b"")))
    crc = crc16_ccitt_false(raw[:-2])
    assert raw[-2] == crc & 0xFF
    assert raw[-1] == crc >> 8


@given(msg_type=types, seq=seqs, payload=payloads)
def test_round_trip_for_payloads_of_0_to_59_bytes(msg_type, seq, payload):
    wire = build_frame(msg_type, seq, payload)
    assert len(wire) <= MAX_WIRE_BYTES
    assert parse_frame(body_of(wire)) == Frame(msg_type, seq, payload)


@given(msg_type=types, seq=seqs, payload=payloads)
def test_no_zero_byte_inside_an_encoded_body(msg_type, seq, payload):
    wire = build_frame(msg_type, seq, payload)
    assert 0 not in wire[:-1]
    assert wire[-1] == 0
    assert wire.count(0) == 1


def test_largest_frame_is_66_bytes_on_the_wire():
    wire = build_frame(0x20, 1, bytes(MAX_PAYLOAD_BYTES))
    assert len(wire) == 66
    assert parse_frame(body_of(wire)) == Frame(0x20, 1, bytes(MAX_PAYLOAD_BYTES))


def test_smallest_frame_has_a_5_byte_raw_form():
    wire = build_frame(0x06, 0, b"")
    assert len(cobs_decode(body_of(wire))) == 5
    assert parse_frame(body_of(wire)) == Frame(0x06, 0, b"")


@pytest.mark.parametrize(
    ("msg_type", "seq", "payload"),
    [(256, 0, b""), (-1, 0, b""), (0, 65536, b""), (0, -1, b""), (0, 0, bytes(60))],
)
def test_build_frame_refuses_values_that_do_not_fit(msg_type, seq, payload):
    with pytest.raises(ValueError):
        build_frame(msg_type, seq, payload)


# ---- failures in protocol order ---------------------------------------------------------


def test_oversize_more_than_66_wire_bytes_is_bad_framing():
    # 66 non-zero bytes plus the delimiter is 67 wire bytes.
    body = bytes([0x41]) * (MAX_BODY_BYTES + 1)
    assert parse_frame(body) is FrameError.BAD_FRAMING


def test_66_wire_bytes_including_the_delimiter_is_the_limit():
    # Stricter reading chosen: the 0x00 delimiter counts, so a body of 65 is the most allowed.
    assert MAX_BODY_BYTES + 1 == MAX_WIRE_BYTES
    body = cobs_encode(raw_frame(0x20, 1, bytes(59)))
    assert len(body) == MAX_BODY_BYTES
    assert isinstance(parse_frame(body), Frame)


def test_decoded_frame_above_64_bytes_is_bad_framing_even_with_a_good_crc():
    raw = raw_frame(0x20, 1, bytes(60))  # 65 raw bytes, valid CRC
    assert len(raw) == 65
    assert parse_frame(cobs_encode(raw)) is FrameError.BAD_FRAMING


def test_decoded_frame_of_exactly_64_bytes_is_accepted():
    raw = raw_frame(0x20, 1, bytes(59))
    assert len(raw) == MAX_RAW_FRAME_BYTES
    assert isinstance(parse_frame(cobs_encode(raw)), Frame)


def test_empty_body_is_bad_framing():
    assert parse_frame(b"") is FrameError.BAD_FRAMING


def test_invalid_cobs_is_bad_framing():
    assert parse_frame(b"\x09\x01\x02") is FrameError.BAD_FRAMING


def test_zero_inside_a_body_is_bad_framing():
    assert parse_frame(b"\x03\x00\x01") is FrameError.BAD_FRAMING


@pytest.mark.parametrize("length", [0, 1, 2, 3, 4])
def test_decoded_frames_shorter_than_5_bytes_are_truncated(length):
    assert parse_frame(cobs_encode(bytes([0x11] * length))) is FrameError.TRUNCATED


def test_truncation_is_reported_before_the_crc_check():
    # 4 bytes that could never carry a valid CRC: still TRUNCATED, not BAD_CRC.
    assert parse_frame(cobs_encode(b"\x01\x02\x03\x04")) is FrameError.TRUNCATED


def test_flipped_bit_in_the_raw_frame_is_bad_crc():
    raw = bytearray(raw_frame(0x02, 9, b"\x10\x27\xd0\x07"))
    raw[4] ^= 0x01
    assert parse_frame(cobs_encode(bytes(raw))) is FrameError.BAD_CRC


def test_wrong_crc_bytes_are_bad_crc():
    raw = bytearray(raw_frame(0x02, 9, b"\x10\x27\xd0\x07"))
    raw[-1] ^= 0xFF
    assert parse_frame(cobs_encode(bytes(raw))) is FrameError.BAD_CRC


@pytest.mark.parametrize("payload", [b"", b"\x00\x00\x00", b"\x01\x10\x20\x30\x40\x50", bytes(59)])
def test_every_single_bit_flip_on_the_wire_is_rejected(payload):
    body = bytearray(body_of(build_frame(0x12, 513, payload)))
    for index in range(len(body) * 8):
        damaged = bytearray(body)
        damaged[index // 8] ^= 1 << (index % 8)
        assert isinstance(parse_frame(bytes(damaged)), FrameError), index


@given(st.binary(min_size=0, max_size=120))
@settings(derandomize=True, max_examples=300)
def test_garbage_never_crashes_and_a_message_needs_a_correct_crc(garbage):
    result = parse_frame(garbage)
    assert isinstance(result, Frame | FrameError)
    if isinstance(result, Frame):
        assert build_frame(result.type, result.seq, result.payload)[:-1] == garbage


# ---- stream reader and resynchronisation ------------------------------------------------


def test_reader_returns_a_frame_after_its_delimiter():
    reader = FrameReader()
    wire = build_frame(0x04, 3, b"\x01\x00\x00\x00\x00")
    assert reader.feed(wire[:-1]) == []
    assert reader.feed(wire[-1:]) == [Frame(0x04, 3, b"\x01\x00\x00\x00\x00")]


def test_reader_handles_several_frames_in_one_chunk_and_byte_by_byte():
    wires = [build_frame(0x30, n, b"") for n in range(5)]
    joined = b"".join(wires)
    expected = [Frame(0x30, n, b"") for n in range(5)]
    assert FrameReader().feed(joined) == expected
    reader = FrameReader()
    collected = []
    for byte in joined:
        collected += reader.feed(bytes([byte]))
    assert collected == expected


def test_reader_resynchronises_at_the_next_zero_after_garbage():
    reader = FrameReader()
    good = build_frame(0x05, 11, b"")
    results = reader.feed(b"\x55\xaa\x13\x37" + b"\x00" + good)
    assert results[-1] == Frame(0x05, 11, b"")
    assert all(isinstance(item, FrameError) for item in results[:-1])
    assert len(results) == 2


def test_reader_resynchronises_after_starting_in_the_middle_of_a_frame():
    good = build_frame(0x04, 1, b"\x01\x02\x03\x04\x05")
    reader = FrameReader()
    results = reader.feed(good[4:] + good)
    assert results[-1] == Frame(0x04, 1, b"\x01\x02\x03\x04\x05")
    assert isinstance(results[0], FrameError)


def test_reader_reports_an_oversize_run_once_and_then_recovers():
    reader = FrameReader()
    good = build_frame(0x06, 2, b"")
    results = reader.feed(bytes([0x41]) * 500 + b"\x00" + good)
    assert results == [FrameError.BAD_FRAMING, Frame(0x06, 2, b"")]


def test_reader_oversize_run_split_across_chunks_still_one_error():
    reader = FrameReader()
    out = reader.feed(bytes([0x41]) * 40)
    out += reader.feed(bytes([0x41]) * 40)
    out += reader.feed(b"\x00")
    assert out == [FrameError.BAD_FRAMING]


def test_reader_ignores_empty_blocks_between_delimiters():
    reader = FrameReader()
    good = build_frame(0x06, 2, b"")
    assert reader.feed(b"\x00\x00\x00" + good + b"\x00\x00") == [Frame(0x06, 2, b"")]


def test_reader_reports_each_failure_kind_separately():
    reader = FrameReader()
    truncated = cobs_encode(b"\x01\x02\x03\x04") + b"\x00"
    damaged = bytearray(raw_frame(0x30, 1, b""))
    damaged[0] ^= 0x04
    bad_crc = cobs_encode(bytes(damaged)) + b"\x00"
    results = reader.feed(truncated + bad_crc + b"\x09\x01\x00")
    assert results == [FrameError.TRUNCATED, FrameError.BAD_CRC, FrameError.BAD_FRAMING]


@given(chunks=st.lists(st.binary(max_size=90), max_size=8))
@settings(derandomize=True, max_examples=200)
def test_reader_never_crashes_on_arbitrary_bytes(chunks):
    reader = FrameReader()
    for chunk in chunks:
        for item in reader.feed(chunk):
            assert isinstance(item, Frame | FrameError)
