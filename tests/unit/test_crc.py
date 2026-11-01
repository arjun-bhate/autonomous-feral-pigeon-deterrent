"""Tests for CRC-16 and CRC-32 (T005)."""

import struct

import pytest
from hypothesis import given
from hypothesis import strategies as st

from deterrent.protocol.crc import crc16_ccitt_false, crc32, zone_bytes, zone_crc32

CHECK_INPUT = b"123456789"


def test_crc16_standard_check_value():
    assert crc16_ccitt_false(CHECK_INPUT) == 0x29B1


def test_crc16_of_nothing_is_the_initial_value():
    assert crc16_ccitt_false(b"") == 0xFFFF


def test_crc16_known_single_byte_values():
    # Hand-checkable: one byte through the same algorithm.
    assert crc16_ccitt_false(b"\x00") == 0xE1F0
    assert crc16_ccitt_false(b"A") == 0xB915


def test_crc32_standard_check_value():
    assert crc32(CHECK_INPUT) == 0xCBF43926


def test_crc32_of_nothing_is_zero():
    assert crc32(b"") == 0


def test_zone_bytes_layout_is_id_count_then_little_endian_points():
    data = zone_bytes(3, [(100, -200), (0x1234, 8000), (-1, 24000)])
    expected = bytes([3, 3]) + struct.pack("<hhhhhh", 100, -200, 0x1234, 8000, -1, 24000)
    assert data == expected
    assert data[:2] == b"\x03\x03"
    assert data[2:6] == b"\x64\x00\x38\xff"  # 100 and -200 as little-endian i16


def test_zone_crc32_matches_a_hand_built_byte_string():
    hand_built = bytes(
        [1, 3, 0x00, 0x00, 0x00, 0x00, 0xE8, 0x03, 0x00, 0x00, 0x00, 0x00, 0xE8, 0x03]
    )
    assert zone_crc32(1, [(0, 0), (1000, 0), (0, 1000)]) == crc32(hand_built)


def test_zone_crc32_changes_with_id_and_with_point_order():
    base = zone_crc32(1, [(0, 0), (1000, 0), (0, 1000)])
    assert zone_crc32(2, [(0, 0), (1000, 0), (0, 1000)]) != base
    assert zone_crc32(1, [(1000, 0), (0, 0), (0, 1000)]) != base


def test_zone_bytes_rejects_values_that_do_not_fit():
    with pytest.raises(ValueError):
        zone_bytes(256, [(0, 0)])
    with pytest.raises(ValueError):
        zone_bytes(1, [(40000, 0)])
    with pytest.raises(ValueError):
        zone_bytes(1, [(0, 0)] * 256)


@given(data=st.binary(min_size=1, max_size=64), bit=st.data())
def test_crc16_always_detects_a_single_flipped_bit(data, bit):
    index = bit.draw(st.integers(min_value=0, max_value=len(data) * 8 - 1))
    flipped = bytearray(data)
    flipped[index // 8] ^= 1 << (index % 8)
    assert crc16_ccitt_false(bytes(flipped)) != crc16_ccitt_false(data)


@given(data=st.binary(min_size=1, max_size=64), bit=st.data())
def test_crc32_always_detects_a_single_flipped_bit(data, bit):
    index = bit.draw(st.integers(min_value=0, max_value=len(data) * 8 - 1))
    flipped = bytearray(data)
    flipped[index // 8] ^= 1 << (index % 8)
    assert crc32(bytes(flipped)) != crc32(data)


@given(
    zone_id=st.integers(0, 255),
    points=st.lists(
        st.tuples(st.integers(-32768, 32767), st.integers(-32768, 32767)), min_size=1, max_size=16
    ),
)
def test_zone_crc32_is_the_crc_of_zone_bytes(zone_id, points):
    assert zone_crc32(zone_id, points) == crc32(zone_bytes(zone_id, points))
