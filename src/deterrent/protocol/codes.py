"""Every number and name that appears on the wire.

Message types, reply results, reject codes, state codes, fault bits, event codes and
the fire-denial ladder. Each is an *enum*: a fixed list of named constants, so the
rest of the code writes ``State.DETER`` instead of a bare 8.

Two numbering spaces exist and are deliberately kept apart:

* **Denial reasons** (``DenyReason``): why a fire request was denied. Each has a
  *permanent code* (sent on the wire, never changed) and a *priority* (the order the
  conditions are checked; may change). They are separate data on purpose.
* **Reject codes** (``RejectCode``): why any other message was rejected.

The same name may appear in both with different numbers (KEY_OFF is 5 as a denial
reason and 13 as a reject code). Uniqueness is per table.

Source of truth: protocol.md sections 4, 6 and 7. ``protocol/reason-codes.json``
mirrors the denial table for tools that read JSON.
"""

from dataclasses import dataclass
from enum import IntEnum, IntFlag, StrEnum


class MsgType(IntEnum):
    """Message types sent from the host (laptop) to the device (protocol.md 4.1)."""

    HOME = 0x01
    MOVE = 0x02
    JOG = 0x03
    HEARTBEAT = 0x04
    ARM = 0x05
    DISARM = 0x06
    ZONE_BEGIN = 0x10
    ZONE_POINT = 0x11
    ZONE_COMMIT = 0x12
    ZONE_DELETE = 0x13
    ZONE_LIST = 0x14
    ZONE_CLEAR = 0x15
    FIRE_REQUEST = 0x20
    FIRE_STOP = 0x21
    STATUS_REQUEST = 0x30


class EventType(IntEnum):
    """Messages the device sends on its own, not as a reply (protocol.md 4.2)."""

    STATUS = 0xC0
    REJECT_NOTICE = 0xC1
    EFFECTOR_EVENT = 0xC2


GENERIC_REJECT = 0xFE  # reply to a message whose type is unknown
_REPLY_FLAG = 0x80


def reply_type(request_type: int) -> int:
    """The type byte of the reply to ``request_type``: the request type with bit 7 set."""
    return request_type | _REPLY_FLAG


class Result(IntEnum):
    """First byte of a reply payload (protocol.md 4.2)."""

    OK = 0
    REJECTED = 1
    GRANTED = 2
    DENIED = 3


class EffectorEvent(IntEnum):
    """The ``event`` byte of an EFFECTOR_EVENT message."""

    ON = 1
    NORMAL_END = 2
    CUT = 3
    STOPPED_BY_HOST = 4


class State(IntEnum):
    """The twelve device states (protocol.md 4.3). The numbers never change."""

    BOOT = 0
    HOMING = 1
    DISARMED = 2
    SCANNING = 3
    CANDIDATE = 4
    SLEW = 5
    CONFIRM = 6
    TRACK = 7
    DETER = 8
    COOLDOWN = 9
    ZONE_SETUP = 10
    SAFE_ERROR = 11


# Names for people. SAFE_ERROR is written SAFE/ERROR in the system documents.
STATE_DISPLAY_NAMES: dict[State, str] = {
    state: ("SAFE/ERROR" if state is State.SAFE_ERROR else state.name) for state in State
}


class Fault(IntFlag):
    """Bits of the 16-bit ``fault_mask`` (protocol.md 4.4). Several can be set at once."""

    HEARTBEAT_LOST = 1 << 0
    UNUSED_BIT_1 = 1 << 1  # was ZONE_STORE_INVALID; validity is derived, never latched
    HOMING_FAILED = 1 << 2  # reserved for feature 003
    INTERNAL = 1 << 3  # reserved for feature 003
    KEY_OFF = 1 << 4


# The only fault bits the device sets by itself in this feature.
SETTABLE_FAULTS = Fault.HEARTBEAT_LOST | Fault.KEY_OFF


class RejectCode(IntEnum):
    """Why a message was rejected (protocol.md section 7). Separate from DenyReason."""

    BAD_FRAMING = 1
    BAD_CRC = 2
    TRUNCATED = 3
    UNKNOWN_TYPE = 4
    BAD_LENGTH = 5
    STALE_SEQ = 6
    FIELD_RANGE = 7
    NOT_HOMED = 10
    BUSY_HOMING = 11
    OUT_OF_SOFT_LIMIT = 12
    KEY_OFF = 13
    FAULT_ACTIVE = 14
    WRONG_STATE = 15
    ZONE_NO_UPLOAD = 20
    ZONE_BAD_INDEX = 21
    ZONE_INCOMPLETE = 22
    ZONE_BAD_CHECKSUM = 23
    ZONE_TOO_FEW_POINTS = 24
    ZONE_TOO_MANY_POINTS = 25
    ZONE_SELF_CROSSING = 26
    ZONE_DEGENERATE = 27
    ZONE_DUPLICATE_ID = 28
    ZONE_STORE_FULL = 29
    ZONE_UNKNOWN_ID = 30
    ZONE_STORAGE_ERROR = 31


class DenyReason(IntEnum):
    """The permanent code of each fire-denial reason (protocol.md section 6).

    A code is never reused or reassigned, even after its reason is retired. A new
    reason gets the next unused number (``next_unused_denial_code``).
    """

    HEARTBEAT_LOST = 1
    FAULT_ACTIVE = 2
    ZONE_SETUP = 3
    NOT_HOMED = 4
    KEY_OFF = 5
    NOT_ARMED = 6
    PERSON_NOT_CLEAR = 7
    REQUEST_EXPIRED = 8
    ZONE_STORE_INVALID = 9
    ELEVATION_LIMIT = 10
    IN_ZONE_MARGIN = 11
    ON_TIME_LIMIT = 12
    RATE_LIMIT = 13  # retired, never returned, never reused
    PERSON_CLEAR_DELAY = 14
    COOLDOWN_ACTIVE = 15


class ReasonStatus(StrEnum):
    ACTIVE = "active"
    RETIRED = "retired"


@dataclass(frozen=True)
class DenialReason:
    """One row of the denial table: a name, its permanent code and its checking order."""

    name: str
    code: int
    priority: int | None  # position in the arbitration order; None for a retired reason
    status: ReasonStatus


# The checking order (priority 1 first). The priority is only this position; the code
# comes from DenyReason and does not depend on the order.
_PRIORITY_ORDER: tuple[DenyReason, ...] = (
    DenyReason.HEARTBEAT_LOST,
    DenyReason.FAULT_ACTIVE,
    DenyReason.ZONE_SETUP,
    DenyReason.NOT_HOMED,
    DenyReason.KEY_OFF,
    DenyReason.NOT_ARMED,
    DenyReason.PERSON_NOT_CLEAR,
    DenyReason.PERSON_CLEAR_DELAY,
    DenyReason.REQUEST_EXPIRED,
    DenyReason.ZONE_STORE_INVALID,
    DenyReason.ELEVATION_LIMIT,
    DenyReason.IN_ZONE_MARGIN,
    DenyReason.ON_TIME_LIMIT,
    DenyReason.COOLDOWN_ACTIVE,
)

DENIAL_LADDER: tuple[DenialReason, ...] = (
    *(
        DenialReason(reason.name, int(reason), position, ReasonStatus.ACTIVE)
        for position, reason in enumerate(_PRIORITY_ORDER, start=1)
    ),
    DenialReason(
        DenyReason.RATE_LIMIT.name, int(DenyReason.RATE_LIMIT), None, ReasonStatus.RETIRED
    ),
)


def next_unused_denial_code() -> int:
    """The code a newly added denial reason must take (retired codes count as used)."""
    return max(int(reason) for reason in DenyReason) + 1
