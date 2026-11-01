"""Protocol messages: how each message is turned into bytes and back.

Every message is a small frozen *dataclass* (a class that is just a bundle of named
fields). Its fields, in order, are exactly the payload fields of protocol.md
section 4.1, and ``_FORMAT`` is a ``struct`` format string describing the bytes
(``<`` little-endian, ``B`` u8, ``H`` u16, ``I`` u32, ``h`` i16).

Part 1: host-to-device messages. Decoding follows the receiver steps of
protocol.md section 2: step 5 (type known), step 6 (payload length exact), step 7
(field values in range). A failure is returned as a ``RejectCode``; the decoder
never raises on bad input from the wire.

Part 2 (further down): device-to-host replies, the status block and the unsolicited
events. Encoding refuses to produce anything the protocol forbids (for example the
retired denial code 13); decoding still accepts such bytes so that a test runner can
detect and report them.
"""

import struct
from dataclasses import astuple, dataclass
from typing import ClassVar

from deterrent.config import MAX_TTL_MS, MIN_DURATION_MS
from deterrent.protocol.codes import (
    DENIAL_LADDER,
    GENERIC_REJECT,
    DenyReason,
    EffectorEvent,
    EventType,
    MsgType,
    ReasonStatus,
    RejectCode,
    Result,
    State,
    reply_type,
)
from deterrent.protocol.framing import build_frame


class _Message:
    """Shared encode/decode behaviour. Subclasses set ``TYPE`` and ``_FORMAT``."""

    TYPE: ClassVar[int]
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


# ---------------------------------------------------------------------------------------
# Part 2: device to host
# ---------------------------------------------------------------------------------------

MAX_REPLY_DATA_BYTES = 57  # 59-byte payload minus the result and code bytes
MAX_ZONES_IN_LIST = 9  # 1 count byte + 6 bytes per zone must fit MAX_REPLY_DATA_BYTES

_RETIRED_DENIAL_CODE = int(DenyReason.RATE_LIMIT)
_ACTIVE_DENIAL_CODES = frozenset(
    entry.code for entry in DENIAL_LADDER if entry.status is ReasonStatus.ACTIVE
)


def _unpack_exact(fmt: str, data: bytes) -> tuple:
    if len(data) != struct.calcsize(fmt):
        raise ValueError(f"expected {struct.calcsize(fmt)} bytes of reply data, got {len(data)}")
    return struct.unpack(fmt, data)


@dataclass(frozen=True)
class Reply:
    """The device's answer to one host message.

    The wire type is the request type with bit 7 set. The payload is ``result``,
    ``code``, then ``data`` whose layout depends on the request (see the pack and
    unpack helpers below).
    """

    request_type: int
    result: Result
    code: int
    data: bytes = b""

    @classmethod
    def ok(cls, request_type: int, data: bytes = b"") -> "Reply":
        return cls(request_type, Result.OK, 0, data)

    @classmethod
    def rejected(cls, request_type: int, code: RejectCode) -> "Reply":
        return cls(request_type, Result.REJECTED, int(code))

    @classmethod
    def granted(cls, granted_on_ms: int) -> "Reply":
        return cls(MsgType.FIRE_REQUEST, Result.GRANTED, 0, struct.pack("<I", granted_on_ms))

    @classmethod
    def denied(cls, reason: DenyReason, fault_mask: int = 0) -> "Reply":
        """A fire denial. The code is the reason's permanent code, never its priority."""
        return cls(MsgType.FIRE_REQUEST, Result.DENIED, int(reason), struct.pack("<H", fault_mask))

    @property
    def wire_type(self) -> int:
        return reply_type(self.request_type)

    def encode_payload(self) -> bytes:
        if self.request_type not in _HOST_BY_TYPE:
            raise ValueError(f"no host message has type {self.request_type:#04x}")
        if len(self.data) > MAX_REPLY_DATA_BYTES:
            raise ValueError(f"reply data is longer than {MAX_REPLY_DATA_BYTES} bytes")
        if self.result is Result.DENIED and self.code == _RETIRED_DENIAL_CODE:
            raise ValueError("denial code 13 is retired and must never be sent")
        return bytes([self.result, self.code]) + self.data

    @classmethod
    def decode_payload(cls, request_type: int, payload: bytes) -> "Reply | RejectCode":
        if len(payload) < 2 or len(payload) - 2 > MAX_REPLY_DATA_BYTES:
            return RejectCode.BAD_LENGTH
        try:
            result = Result(payload[0])
        except ValueError:
            return RejectCode.FIELD_RANGE
        return cls(request_type, result, payload[1], bytes(payload[2:]))


@dataclass(frozen=True)
class GenericReject(_Message):
    """Reply to a message whose type is unknown, so no normal reply type exists (0xFE)."""

    TYPE: ClassVar[int] = GENERIC_REJECT
    _FORMAT: ClassVar[str] = "<BBB"
    request_type: int

    def encode_payload(self) -> bytes:
        return struct.pack(
            self._FORMAT, Result.REJECTED, RejectCode.UNKNOWN_TYPE, self.request_type
        )

    @classmethod
    def _fields_in_range(cls, values: tuple[int, ...]) -> bool:
        return values[0] == Result.REJECTED and values[1] == RejectCode.UNKNOWN_TYPE

    @classmethod
    def _build(cls, values: tuple[int, ...]):
        return cls(values[2])


@dataclass(frozen=True)
class StatusBlock(_Message):
    """The 13-byte device status, used in the STATUS_REQUEST reply and the STATUS event."""

    _FORMAT: ClassVar[str] = "<BhhBBBBBHB"
    state: State
    pan: int
    tilt: int
    key_on: bool
    armed: bool
    homed: bool
    zone_store_valid: bool
    effector_on: bool
    fault_mask: int
    zone_count: int

    @classmethod
    def _fields_in_range(cls, values: tuple[int, ...]) -> bool:
        return values[0] in {int(state) for state in State} and all(
            flag in (0, 1) for flag in values[3:8]
        )

    @classmethod
    def _build(cls, values: tuple[int, ...]):
        state, pan, tilt, key_on, armed, homed, valid, effector, faults, zones = values
        return cls(
            State(state),
            pan,
            tilt,
            bool(key_on),
            bool(armed),
            bool(homed),
            bool(valid),
            bool(effector),
            faults,
            zones,
        )


@dataclass(frozen=True)
class StatusEvent(_Message):
    """Unsolicited STATUS event (0xC0): a status block, sent on change and every second."""

    TYPE: ClassVar[int] = EventType.STATUS
    status: StatusBlock

    def encode_payload(self) -> bytes:
        return self.status.encode_payload()

    @classmethod
    def decode_payload(cls, payload: bytes) -> "StatusEvent | RejectCode":
        status = StatusBlock.decode_payload(payload)
        return status if isinstance(status, RejectCode) else cls(status)


@dataclass(frozen=True)
class RejectNotice(_Message):
    """Unsolicited REJECT_NOTICE (0xC1): a frame was dropped at steps 1 to 3."""

    TYPE: ClassVar[int] = EventType.REJECT_NOTICE
    _FORMAT: ClassVar[str] = "<BH"
    cause: int
    total_count: int


@dataclass(frozen=True)
class EffectorEventMessage(_Message):
    """Unsolicited EFFECTOR_EVENT (0xC2): the effector switched on, ended, was cut or stopped."""

    TYPE: ClassVar[int] = EventType.EFFECTOR_EVENT
    _FORMAT: ClassVar[str] = "<BB"
    event: EffectorEvent
    cause: int

    def encode_payload(self) -> bytes:
        if self.event is EffectorEvent.CUT:
            if self.cause not in _ACTIVE_DENIAL_CODES:
                raise ValueError(f"cut cause {self.cause} is not an active denial code")
        elif self.cause != 0:
            raise ValueError("only a cut carries a cause")
        return super().encode_payload()

    @classmethod
    def _fields_in_range(cls, values: tuple[int, ...]) -> bool:
        return values[0] in {int(event) for event in EffectorEvent}

    @classmethod
    def _build(cls, values: tuple[int, ...]):
        return cls(EffectorEvent(values[0]), values[1])


DeviceMessage = Reply | GenericReject | StatusEvent | RejectNotice | EffectorEventMessage

_EVENT_CLASSES: dict[int, type[_Message]] = {
    EventType.STATUS: StatusEvent,
    EventType.REJECT_NOTICE: RejectNotice,
    EventType.EFFECTOR_EVENT: EffectorEventMessage,
}


def encode_device_message(message: DeviceMessage) -> tuple[int, bytes]:
    """The (type byte, payload bytes) of a device message. Raises ValueError if the
    message breaks a protocol rule (for example denial code 13)."""
    if isinstance(message, Reply):
        return message.wire_type, message.encode_payload()
    return int(message.TYPE), message.encode_payload()


def decode_device_message(msg_type: int, payload: bytes) -> DeviceMessage | RejectCode:
    """Decode a message received by the host. Returns the message or a RejectCode
    (UNKNOWN_TYPE, BAD_LENGTH or FIELD_RANGE)."""
    if msg_type == GENERIC_REJECT:
        return GenericReject.decode_payload(payload)
    event_class = _EVENT_CLASSES.get(msg_type)
    if event_class is not None:
        return event_class.decode_payload(payload)
    if msg_type & 0x80 and (msg_type & 0x7F) in _HOST_BY_TYPE:
        return Reply.decode_payload(msg_type & 0x7F, payload)
    return RejectCode.UNKNOWN_TYPE


def device_frame(message: DeviceMessage, seq: int) -> bytes:
    """A complete wire frame (with delimiter) carrying a device message."""
    msg_type, payload = encode_device_message(message)
    return build_frame(msg_type, seq, payload)


# ---- reply data layouts -----------------------------------------------------------------


def pack_angles(pan: int, tilt: int) -> bytes:
    """Data of a MOVE or JOG reply: the angles after the move."""
    return struct.pack("<hh", pan, tilt)


def unpack_angles(data: bytes) -> tuple[int, int]:
    return _unpack_exact("<hh", data)


def pack_heartbeat_data(device_ms: int, state: State) -> bytes:
    """Data of a HEARTBEAT reply: the device clock and its state."""
    return struct.pack("<IB", device_ms, state)


def unpack_heartbeat_data(data: bytes) -> tuple[int, State]:
    device_ms, state = _unpack_exact("<IB", data)
    return device_ms, State(state)


def unpack_granted_on_ms(data: bytes) -> int:
    """Data of a GRANTED reply: the device time of the grant."""
    return _unpack_exact("<I", data)[0]


def unpack_fault_mask(data: bytes) -> int:
    """Data of a DENIED reply: the fault mask (zero unless the reason is FAULT_ACTIVE)."""
    return _unpack_exact("<H", data)[0]


@dataclass(frozen=True)
class ZoneEntry:
    """One zone in a ZONE_LIST reply."""

    zone_id: int
    point_count: int
    crc32: int


def pack_zone_list(entries: list[ZoneEntry]) -> bytes:
    """Data of a ZONE_LIST reply: a count, then 6 bytes per zone."""
    if len(entries) > MAX_ZONES_IN_LIST:
        raise ValueError(f"a zone list holds at most {MAX_ZONES_IN_LIST} zones")
    body = bytearray([len(entries)])
    for entry in entries:
        body += struct.pack("<BBI", entry.zone_id, entry.point_count, entry.crc32)
    return bytes(body)


def unpack_zone_list(data: bytes) -> list[ZoneEntry]:
    if not data or len(data) != 1 + 6 * data[0]:
        raise ValueError("zone list data does not match its count")
    return [ZoneEntry(*struct.unpack_from("<BBI", data, 1 + 6 * index)) for index in range(data[0])]
