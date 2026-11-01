"""Frames on the wire: COBS encoding, frame building and frame parsing.

A *frame* is one message packed for the serial/Wi-Fi link:

    raw frame   = type (1 byte) | seq (2 bytes) | payload (0..59 bytes) | crc16 (2 bytes)
    on the wire = COBS(raw frame) | 0x00

COBS (Consistent Overhead Byte Stuffing) rewrites the raw bytes so that the value
0x00 never appears inside them. The 0x00 at the end is then an unambiguous "end of
frame" marker, and a receiver that lost its place just throws bytes away until the
next 0x00 and starts again (that is "resynchronising").

This module covers receiver steps 1 to 3 of protocol.md section 2 (framing, length,
CRC). The sequence rule and everything after it belong to the device.
"""

import struct
from dataclasses import dataclass
from enum import StrEnum

from deterrent.protocol.crc import crc16_ccitt_false

MIN_RAW_FRAME_BYTES = 5  # type + seq + crc, empty payload
MAX_RAW_FRAME_BYTES = 64
MAX_PAYLOAD_BYTES = MAX_RAW_FRAME_BYTES - MIN_RAW_FRAME_BYTES  # 59
# A 64-byte raw frame grows by one COBS byte, giving a 65-byte body, plus the 0x00
# delimiter makes 66 bytes on the wire. protocol.md and tasks.md word this limit
# differently; the stricter reading (the delimiter counts) is used: a body longer
# than 65 bytes is rejected.
MAX_BODY_BYTES = 65
MAX_WIRE_BYTES = MAX_BODY_BYTES + 1

_DELIMITER = 0


class CobsError(ValueError):
    """The bytes are not a valid COBS block."""


class FrameError(StrEnum):
    """Why a frame was rejected. The value is the reject-code name in protocol.md section 7."""

    BAD_FRAMING = "BAD_FRAMING"
    TRUNCATED = "TRUNCATED"
    BAD_CRC = "BAD_CRC"


@dataclass(frozen=True)
class Frame:
    """A frame that passed steps 1 to 3: its message type, sequence number and payload."""

    type: int
    seq: int
    payload: bytes


def cobs_encode(data: bytes) -> bytes:
    """COBS-encode ``data``. The result contains no 0x00 byte."""
    out = bytearray([0])  # placeholder for the first block's length byte
    code_index = 0
    code = 1
    for byte in data:
        if byte == 0:
            out[code_index] = code
            code_index = len(out)
            out.append(0)
            code = 1
        else:
            out.append(byte)
            code += 1
            if code == 0xFF:  # a full block of 254 non-zero bytes
                out[code_index] = code
                code_index = len(out)
                out.append(0)
                code = 1
    out[code_index] = code
    return bytes(out)


def cobs_decode(data: bytes) -> bytes:
    """Undo ``cobs_encode``. Raises ``CobsError`` if ``data`` is not valid COBS."""
    out = bytearray()
    index = 0
    length = len(data)
    while index < length:
        code = data[index]
        if code == 0:
            raise CobsError("zero byte inside a COBS block")
        end = index + code
        if end > length:
            raise CobsError("block runs past the end of the data")
        chunk = data[index + 1 : end]
        if _DELIMITER in chunk:
            raise CobsError("zero byte inside a COBS block")
        out += chunk
        index = end
        if code != 0xFF and index < length:
            out.append(0)  # every block except a full one or the last stands for a zero
    return bytes(out)


def build_frame(msg_type: int, seq: int, payload: bytes = b"") -> bytes:
    """Build the bytes to put on the wire for one message, including the 0x00 delimiter."""
    if not 0 <= msg_type <= 0xFF:
        raise ValueError("message type must fit in one byte")
    if not 0 <= seq <= 0xFFFF:
        raise ValueError("sequence number must fit in two bytes")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"payload is longer than {MAX_PAYLOAD_BYTES} bytes")
    head = struct.pack("<BH", msg_type, seq) + payload
    raw = head + struct.pack("<H", crc16_ccitt_false(head))
    return cobs_encode(raw) + bytes([_DELIMITER])


def parse_frame(body: bytes) -> Frame | FrameError:
    """Check one received block (the bytes before the 0x00) and return the frame or the
    first failure, in protocol.md section 2 order: framing, then length, then CRC."""
    # Step 1: a valid COBS block that is not too long.
    if not body or len(body) > MAX_BODY_BYTES:
        return FrameError.BAD_FRAMING
    try:
        raw = cobs_decode(body)
    except CobsError:
        return FrameError.BAD_FRAMING
    if len(raw) > MAX_RAW_FRAME_BYTES:
        return FrameError.BAD_FRAMING
    # Step 2: long enough to hold type, sequence and CRC.
    if len(raw) < MIN_RAW_FRAME_BYTES:
        return FrameError.TRUNCATED
    # Step 3: the CRC at the end matches the rest.
    head, crc_bytes = raw[:-2], raw[-2:]
    if struct.unpack("<H", crc_bytes)[0] != crc16_ccitt_false(head):
        return FrameError.BAD_CRC
    msg_type, seq = struct.unpack("<BH", head[:3])
    return Frame(msg_type, seq, head[3:])


class FrameReader:
    """Turns a stream of received bytes into frames and failures.

    Feed it whatever bytes arrive, in any chunk size. It collects bytes until a 0x00,
    checks the block with ``parse_frame`` and moves on, so garbage never blocks it:
    the next 0x00 always starts a fresh frame. A run of bytes that is already too long
    is dropped as it arrives (memory stays bounded) and reported once as BAD_FRAMING.
    Empty blocks (two 0x00 in a row) carry no message and are ignored.
    """

    def __init__(self) -> None:
        self._block = bytearray()
        self._overflow = False

    def feed(self, data: bytes) -> list[Frame | FrameError]:
        results: list[Frame | FrameError] = []
        for byte in data:
            if byte == _DELIMITER:
                if self._overflow:
                    results.append(FrameError.BAD_FRAMING)
                elif self._block:
                    results.append(parse_frame(bytes(self._block)))
                self._block.clear()
                self._overflow = False
            elif not self._overflow:
                self._block.append(byte)
                if len(self._block) > MAX_BODY_BYTES:
                    self._overflow = True
                    self._block.clear()
        return results
