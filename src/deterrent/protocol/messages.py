"""Protocol messages: how each message is turned into bytes and back.

Every message is a small frozen *dataclass* (a class that is just a bundle of named
fields). Its fields, in order, are exactly the payload fields of protocol.md
section 4.1, and ``_FORMAT`` is a ``struct`` format string describing the bytes
(``<`` little-endian, ``B`` u8, ``H`` u16, ``I`` u32, ``h`` i16).

Part 1 (this section): host-to-device messages. Decoding follows the receiver
steps of protocol.md section 2: step 5 (type known), step 6 (payload length exact),
step 7 (field values in range). A failure is returned as a ``RejectCode``; the
decoder never raises on bad input from the wire.
"""

import struct
from dataclasses import astuple, dataclass
from typing import ClassVar

from deterrent.config import MAX_TTL_MS, MIN_DURATION_MS
from deterrent.protocol.codes import MsgType, RejectCode
from deterrent.protocol.framing import build_frame


class _Message:
    """Shared encode/decode behaviour. Subclasses set ``TYPE`` and ``_FORMAT``."""

    TYPE: ClassVar[MsgType]
    _FORMAT: ClassVar[str] = "<"

    def encode_payload(self) -> bytes:
        try:
            return struct.pack(self._FORMAT, *astuple(self))
        except struct.error as error:
            raise ValueError(f"{type(self).__name__}: a field does not fit its size") from error

    @classmethod
    def _fields_in_range(cls, values: tuple[int, ...]) -> bool:
        """Step 7: are the raw field values acceptable? Most fields accept any value."""
        return True

    @classmethod
    def _build(cls, values: tuple[int, ...]):
        return cls(*values)

    @classmethod
    def decode_payload(cls, payload: bytes):
        """Steps 6 and 7: exact length, then field ranges. Returns the message or a RejectCode."""
        if len(payload) != struct.calcsize(cls._FORMAT):
            return RejectCode.BAD_LENGTH
        values = struct.unpack(cls._FORMAT, payload)
        if not cls._fields_in_range(values):
            return RejectCode.FIELD_RANGE
        return cls._build(values)


@dataclass(frozen=True)
class Home(_Message):
    TYPE: ClassVar[MsgType] = MsgType.HOME


@dataclass(frozen=True)
class Move(_Message):
    """Go to an absolute position (centidegrees)."""

    TYPE: ClassVar[MsgType] = MsgType.MOVE
    _FORMAT: ClassVar[str] = "<hh"
    pan: int
    tilt: int


@dataclass(frozen=True)
class Jog(_Message):
    """Move by a relative amount (centidegrees)."""

    TYPE: ClassVar[MsgType] = MsgType.JOG
    _FORMAT: ClassVar[str] = "<hh"
    dpan: int
    dtilt: int


@dataclass(frozen=True)
class Heartbeat(_Message):
    """The host is alive, and whether the camera sees no person (``person_clear``).

    ``laptop_ts_ms`` is carried for logging only and is never used for timing.
    """

    TYPE: ClassVar[MsgType] = MsgType.HEARTBEAT
    _FORMAT: ClassVar[str] = "<BI"
    person_clear: bool
    laptop_ts_ms: int

    @classmethod
    def _fields_in_range(cls, values: tuple[int, ...]) -> bool:
        return values[0] in (0, 1)

    @classmethod
    def _build(cls, values: tuple[int, ...]):
        return cls(bool(values[0]), values[1])


@dataclass(frozen=True)
class Arm(_Message):
    TYPE: ClassVar[MsgType] = MsgType.ARM


@dataclass(frozen=True)
class Disarm(_Message):
    TYPE: ClassVar[MsgType] = MsgType.DISARM


@dataclass(frozen=True)
class ZoneBegin(_Message):
    TYPE: ClassVar[MsgType] = MsgType.ZONE_BEGIN
    _FORMAT: ClassVar[str] = "<BB"
    zone_id: int
    point_count: int


@dataclass(frozen=True)
class ZonePoint(_Message):
    TYPE: ClassVar[MsgType] = MsgType.ZONE_POINT
    _FORMAT: ClassVar[str] = "<Bhh"
    index: int
    pan: int
    tilt: int


@dataclass(frozen=True)
class ZoneCommit(_Message):
    TYPE: ClassVar[MsgType] = MsgType.ZONE_COMMIT
    _FORMAT: ClassVar[str] = "<I"
    crc32: int


@dataclass(frozen=True)
class ZoneDelete(_Message):
    TYPE: ClassVar[MsgType] = MsgType.ZONE_DELETE
    _FORMAT: ClassVar[str] = "<B"
    zone_id: int


@dataclass(frozen=True)
class ZoneList(_Message):
    TYPE: ClassVar[MsgType] = MsgType.ZONE_LIST


@dataclass(frozen=True)
class ZoneClear(_Message):
    TYPE: ClassVar[MsgType] = MsgType.ZONE_CLEAR


@dataclass(frozen=True)
class FireRequest(_Message):
    """Ask the device to fire. ``duration_ms`` is the maximum on-time of this shot."""

    TYPE: ClassVar[MsgType] = MsgType.FIRE_REQUEST
    _FORMAT: ClassVar[str] = "<IHhhH"
    issued_at_ms: int
    ttl_ms: int
    intended_pan: int
    intended_tilt: int
    duration_ms: int

    @classmethod
    def _fields_in_range(cls, values: tuple[int, ...]) -> bool:
        _issued_at, ttl_ms, _pan, _tilt, duration_ms = values
        # No "hold" value: below the minimum, including 0, is out of range. Too long a
        # duration is not a range error; arbitration denies it as ON_TIME_LIMIT.
        return 1 <= ttl_ms <= MAX_TTL_MS and duration_ms >= MIN_DURATION_MS


@dataclass(frozen=True)
class FireStop(_Message):
    """Stop a running shot. Has no payload."""

    TYPE: ClassVar[MsgType] = MsgType.FIRE_STOP


@dataclass(frozen=True)
class StatusRequest(_Message):
    TYPE: ClassVar[MsgType] = MsgType.STATUS_REQUEST


HostMessage = (
    Home
    | Move
    | Jog
    | Heartbeat
    | Arm
    | Disarm
    | ZoneBegin
    | ZonePoint
    | ZoneCommit
    | ZoneDelete
    | ZoneList
    | ZoneClear
    | FireRequest
    | FireStop
    | StatusRequest
)

_HOST_CLASSES: tuple[type[_Message], ...] = (
    Home,
    Move,
    Jog,
    Heartbeat,
    Arm,
    Disarm,
    ZoneBegin,
    ZonePoint,
    ZoneCommit,
    ZoneDelete,
    ZoneList,
    ZoneClear,
    FireRequest,
    FireStop,
    StatusRequest,
)
_HOST_BY_TYPE: dict[int, type[_Message]] = {cls.TYPE: cls for cls in _HOST_CLASSES}


def encode_host_message(message: HostMessage) -> tuple[int, bytes]:
    """The (type byte, payload bytes) of a host message."""
    return int(message.TYPE), message.encode_payload()


def decode_host_message(msg_type: int, payload: bytes) -> HostMessage | RejectCode:
    """Steps 5 to 7 of protocol.md section 2 for a message received by the device.

    Returns the message, or ``UNKNOWN_TYPE``, ``BAD_LENGTH`` or ``FIELD_RANGE``
    (the first that applies, in that order).
    """
    message_class = _HOST_BY_TYPE.get(msg_type)
    if message_class is None:
        return RejectCode.UNKNOWN_TYPE
    return message_class.decode_payload(payload)


def host_frame(message: HostMessage, seq: int) -> bytes:
    """A complete wire frame (with delimiter) carrying ``message`` with sequence ``seq``."""
    msg_type, payload = encode_host_message(message)
    return build_frame(msg_type, seq, payload)
