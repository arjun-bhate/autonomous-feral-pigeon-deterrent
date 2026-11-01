"""Checksums used by the protocol.

CRC-16 protects every frame on the wire. CRC-32 protects a stored zone, so the
device can notice if the bytes of a zone have been damaged. A CRC (cyclic
redundancy check) is a short number computed from a block of bytes; if even one
bit of the block changes, the number changes, so a receiver can detect damage.
"""

import struct
import zlib
from collections.abc import Sequence

_CRC16_POLY = 0x1021
_CRC16_INIT = 0xFFFF


def _build_crc16_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        value = byte << 8
        for _ in range(8):
            value = ((value << 1) ^ _CRC16_POLY) if value & 0x8000 else (value << 1)
        table.append(value & 0xFFFF)
    return tuple(table)


_CRC16_TABLE = _build_crc16_table()


def crc16_ccitt_false(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: polynomial 0x1021, start 0xFFFF, no reflection, no final XOR."""
    crc = _CRC16_INIT
    for byte in data:
        crc = ((crc << 8) & 0xFFFF) ^ _CRC16_TABLE[((crc >> 8) ^ byte) & 0xFF]
    return crc


def crc32(data: bytes) -> int:
    """CRC-32 (IEEE 802.3), identical to the one in Python's zlib."""
    return zlib.crc32(data) & 0xFFFFFFFF


def zone_bytes(zone_id: int, points: Sequence[tuple[int, int]]) -> bytes:
    """The bytes a zone checksum is computed over (protocol.md section 5.5).

    zone_id (1 byte), point count (1 byte), then pan and tilt of each point as
    signed 16-bit little-endian integers.
    """
    if not 0 <= zone_id <= 255:
        raise ValueError("zone_id must fit in one byte")
    if len(points) > 255:
        raise ValueError("a zone cannot have more than 255 points")
    body = bytearray((zone_id, len(points)))
    for pan, tilt in points:
        try:
            body += struct.pack("<hh", pan, tilt)
        except struct.error as error:
            raise ValueError(f"point ({pan}, {tilt}) does not fit in signed 16 bits") from error
    return bytes(body)


def zone_crc32(zone_id: int, points: Sequence[tuple[int, int]]) -> int:
    """CRC-32 of a zone, as sent in ZONE_COMMIT and kept in the zone store."""
    return crc32(zone_bytes(zone_id, points))
