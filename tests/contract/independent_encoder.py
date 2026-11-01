"""A second, independent way to build protocol messages, written only from PROTOCOL.md.

Why this file exists: the reference encoder in ``src/deterrent/protocol`` and the document
``protocol/PROTOCOL.md`` were written by the same people, so a mistake could hide in both.
A future firmware author will have only the document. This file is what such a person would
write from it, and it does **not import anything from ``deterrent``**. It has its own:

* COBS (the byte-stuffing that removes every 0x00 from a frame, document section 2),
* CRC-16/CCITT-FALSE (section 2) and CRC-32 (section 5.5),
* message layouts (section 4), written with ``int.to_bytes`` rather than ``struct``,
* reading of the document's own tables: message type numbers (4.1), denial reason codes
  (6) and reject codes (7) are taken from PROTOCOL.md at run time, so if a number in the
  document changes, the bytes built here change with it.

``test_hand_built_messages.py`` compares what this builds with the reference encoder.
"""

import re
from dataclasses import dataclass
from pathlib import Path

DOCUMENT = Path(__file__).resolve().parents[2] / "protocol" / "PROTOCOL.md"

# Reply result values, document section 4.2.
OK, REJECTED, GRANTED, DENIED = 0, 1, 2, 3
MAX_PAYLOAD = 59  # a raw frame is at most 64 bytes: 3 header + payload + 2 CRC (section 2)


# ---- reading the document's tables --------------------------------------------------------------


@dataclass(frozen=True)
class Tables:
    message_types: dict[str, int]  # section 4.1: HOME -> 0x01 ...
    denial_codes: dict[str, int]  # section 6: KEY_OFF -> 5 ...
    reject_codes: dict[str, int]  # section 7: ZONE_NO_UPLOAD -> 20 ...


def _between(text: str, start: str, end: str) -> str:
    begin = text.index(start)
    return text[begin : text.index(end, begin)]


def load_tables(text: str | None = None) -> Tables:
    """Read the three tables out of the document (or out of ``text``, for a changed copy)."""
    if text is None:
        text = DOCUMENT.read_text(encoding="utf-8")
    types = {}
    for line in _between(text, "### 4.1 Host to device", "### 4.2 Device to host").splitlines():
        found = re.match(r"\|\s*0x([0-9A-Fa-f]{2})\s*\|\s*([A-Z_]+)\s*\|", line)
        if found:
            types[found.group(2)] = int(found.group(1), 16)
    denials = {}
    section = _between(text, "## 6. Fire denial reasons", "### 6.1 Reason codes are permanent")
    for line in section.splitlines():
        found = re.match(r"\|\s*(?:\d+|none)\s*\|\s*([A-Z_]+)\s*\|\s*(\d+)\s*\|", line)
        if found:
            denials[found.group(1)] = int(found.group(2))
    rejects = {}
    for line in _between(text, "## 7. Reject codes", "## 8. Timing rules").splitlines():
        found = re.match(r"\|\s*(\d+)\s*\|\s*([A-Z_]+)\s*\|", line)
        if found:
            rejects[found.group(2)] = int(found.group(1))
    return Tables(types, denials, rejects)


# ---- byte helpers (little-endian, document section 1) -------------------------------------------


def u8(value: int) -> bytes:
    return value.to_bytes(1, "little")


def u16(value: int) -> bytes:
    return value.to_bytes(2, "little")


def u32(value: int) -> bytes:
    return value.to_bytes(4, "little")


def i16(value: int) -> bytes:
    return value.to_bytes(2, "little", signed=True)


def hexs(data: bytes) -> str:
    return " ".join(f"{byte:02X}" for byte in data)


# ---- checksums ----------------------------------------------------------------------------------


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: polynomial 0x1021, start 0xFFFF, bits not reflected, no final XOR."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def crc32(data: bytes) -> int:
    """CRC-32 as in IEEE 802.3 and zlib: reflected, polynomial 0xEDB88320, start/end 0xFFFFFFFF."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xEDB88320 if crc & 1 else crc >> 1
    return crc ^ 0xFFFFFFFF


def zone_crc32(zone_id: int, points: list[tuple[int, int]]) -> int:
    """Section 5.5: zone_id, point_count, then pan and tilt of each point as i16 little-endian."""
    body = u8(zone_id) + u8(len(points))
    for pan, tilt in points:
        body += i16(pan) + i16(tilt)
    return crc32(body)


# ---- COBS: no zero byte inside a frame, a zero marks the end ------------------------------------


def cobs_encode(data: bytes) -> bytes:
    """Cut the data at every zero. Each piece is sent as (its length + 1) then its bytes; the
    zero that ended it is implied. A piece of 254 bytes is sent as 0xFF then its bytes, with no
    implied zero after it."""
    out = bytearray()
    piece = bytearray()
    for byte in data:
        if byte == 0:
            out.append(len(piece) + 1)
            out += piece
            piece = bytearray()
        else:
            piece.append(byte)
            if len(piece) == 254:
                out.append(0xFF)
                out += piece
                piece = bytearray()
    out.append(len(piece) + 1)
    out += piece
    return bytes(out)


def cobs_decode(data: bytes) -> bytes:
    out = bytearray()
    position = 0
    while position < len(data):
        length = data[position]
        if length == 0 or position + length > len(data):
            raise ValueError("not valid COBS")
        out += data[position + 1 : position + length]
        position += length
        if length != 0xFF and position < len(data):
            out.append(0)
    return bytes(out)


# ---- frames: type | seq | payload | crc16, then COBS and a zero ---------------------------------


def raw_frame(msg_type: int, seq: int, payload: bytes = b"") -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload longer than 59 bytes")
    head = u8(msg_type) + u16(seq) + payload
    return head + u16(crc16(head))  # the CRC goes low byte first


def frame(msg_type: int, seq: int, payload: bytes = b"") -> bytes:
    """The bytes on the wire for one message, including the final 0x00."""
    return cobs_encode(raw_frame(msg_type, seq, payload)) + b"\x00"


def decode(wire: bytes) -> dict:
    """Check and take apart one frame received from the device."""
    if not wire.endswith(b"\x00"):
        raise ValueError("a frame ends with 0x00")
    raw = cobs_decode(wire[:-1])
    if len(raw) < 5:
        raise ValueError("shorter than 5 bytes")
    if crc16(raw[:-2]) != int.from_bytes(raw[-2:], "little"):
        raise ValueError("bad CRC")
    return {"type": raw[0], "seq": int.from_bytes(raw[1:3], "little"), "payload": raw[3:-2]}


def with_wrong_crc(wire: bytes) -> bytes:
    """The same frame with one bit of its last byte before the end marker flipped, which
    damages the checksum (the receiver must then ignore the frame)."""
    damaged = bytearray(wire)
    damaged[-2] ^= 0x01
    if 0 in damaged[:-1]:
        raise ValueError("this flip would put a zero inside the frame")
    return bytes(damaged)


# ---- host messages (document section 4.1) -------------------------------------------------------


def _type(name: str, tables: Tables | None) -> int:
    return (tables or load_tables()).message_types[name]


def fire_request_payload(
    issued_at_ms: int, ttl_ms: int, intended_pan: int, intended_tilt: int, duration_ms: int
) -> bytes:
    return (
        u32(issued_at_ms) + u16(ttl_ms) + i16(intended_pan) + i16(intended_tilt) + u16(duration_ms)
    )


def fire_request(
    seq, issued_at_ms, ttl_ms, intended_pan, intended_tilt, duration_ms, tables=None
) -> bytes:
    payload = fire_request_payload(issued_at_ms, ttl_ms, intended_pan, intended_tilt, duration_ms)
    return frame(_type("FIRE_REQUEST", tables), seq, payload)


def fire_stop(seq: int, tables: Tables | None = None) -> bytes:
    return frame(_type("FIRE_STOP", tables), seq)


def move(seq: int, pan: int, tilt: int, tables: Tables | None = None) -> bytes:
    return frame(_type("MOVE", tables), seq, i16(pan) + i16(tilt))


def zone_begin(seq: int, zone_id: int, point_count: int, tables: Tables | None = None) -> bytes:
    return frame(_type("ZONE_BEGIN", tables), seq, u8(zone_id) + u8(point_count))


def zone_point(seq: int, index: int, pan: int, tilt: int, tables: Tables | None = None) -> bytes:
    return frame(_type("ZONE_POINT", tables), seq, u8(index) + i16(pan) + i16(tilt))


def zone_commit(seq: int, checksum: int, tables: Tables | None = None) -> bytes:
    return frame(_type("ZONE_COMMIT", tables), seq, u32(checksum))


def zone_list(seq: int, tables: Tables | None = None) -> bytes:
    return frame(_type("ZONE_LIST", tables), seq)


# ---- the replies the document says the device sends (section 4.2) -------------------------------


def _reply(request_type: int, seq: int, result: int, code: int, data: bytes = b"") -> bytes:
    """A reply has the request's type with bit 7 set, the request's sequence number, and the
    payload result | code | data."""
    return frame(request_type | 0x80, seq, u8(result) + u8(code) + data)


def predicted_ok(request_type: int, seq: int, data: bytes = b"") -> bytes:
    return _reply(request_type, seq, OK, 0, data)


def predicted_granted(seq: int, granted_on_ms: int, tables: Tables | None = None) -> bytes:
    return _reply(_type("FIRE_REQUEST", tables), seq, GRANTED, 0, u32(granted_on_ms))


def predicted_denied(
    request_type: int, seq: int, reason: str, fault_mask: int = 0, tables: Tables | None = None
) -> bytes:
    """A denial carries the reason's permanent code (document section 6, Code column)."""
    code = (tables or load_tables()).denial_codes[reason]
    return _reply(request_type, seq, DENIED, code, u16(fault_mask))


def predicted_rejected(
    request_type: int, seq: int, reject: str, tables: Tables | None = None
) -> bytes:
    code = (tables or load_tables()).reject_codes[reject]
    return _reply(request_type, seq, REJECTED, code)


def angles_data(pan: int, tilt: int) -> bytes:
    """Reply data of MOVE and JOG: the angles after the move."""
    return i16(pan) + i16(tilt)


def zone_list_data(entries: list[tuple[int, int, int]]) -> bytes:
    """Reply data of ZONE_LIST: a count, then zone_id, point_count and crc32 for each zone."""
    data = u8(len(entries))
    for zone_id, point_count, checksum in entries:
        data += u8(zone_id) + u8(point_count) + u32(checksum)
    return data
