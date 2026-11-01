"""Tests for the protocol identifiers in codes.py and protocol/reason-codes.json (T007)."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from deterrent.protocol.codes import (
    DENIAL_LADDER,
    GENERIC_REJECT,
    SETTABLE_FAULTS,
    STATE_DISPLAY_NAMES,
    DenyReason,
    EffectorEvent,
    EventType,
    Fault,
    MsgType,
    ReasonStatus,
    RejectCode,
    Result,
    State,
    next_unused_denial_code,
    reply_type,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REASON_CODES_FILE = REPO_ROOT / "protocol" / "reason-codes.json"

# protocol.md section 6, written out by hand: name -> (priority, code).
EXPECTED_ACTIVE = {
    "HEARTBEAT_LOST": (1, 1),
    "FAULT_ACTIVE": (2, 2),
    "ZONE_SETUP": (3, 3),
    "NOT_HOMED": (4, 4),
    "KEY_OFF": (5, 5),
    "NOT_ARMED": (6, 6),
    "PERSON_NOT_CLEAR": (7, 7),
    "PERSON_CLEAR_DELAY": (8, 14),
    "REQUEST_EXPIRED": (9, 8),
    "ZONE_STORE_INVALID": (10, 9),
    "ELEVATION_LIMIT": (11, 10),
    "IN_ZONE_MARGIN": (12, 11),
    "ON_TIME_LIMIT": (13, 12),
    "COOLDOWN_ACTIVE": (14, 15),
}


def active_ladder():
    return [entry for entry in DENIAL_LADDER if entry.status is ReasonStatus.ACTIVE]


def retired_ladder():
    return [entry for entry in DENIAL_LADDER if entry.status is ReasonStatus.RETIRED]


# ---- denial table ----------------------------------------------------------------------


def test_ladder_has_14_active_rungs_in_priority_order():
    ladder = active_ladder()
    assert [entry.priority for entry in ladder] == list(range(1, 15))
    assert [entry.name for entry in ladder] == list(EXPECTED_ACTIVE)


def test_every_active_reason_has_the_documented_priority_and_code():
    for entry in active_ladder():
        assert (entry.priority, entry.code) == EXPECTED_ACTIVE[entry.name]


def test_denial_names_are_unique_and_codes_are_unique_and_1_to_255():
    names = [entry.name for entry in DENIAL_LADDER]
    codes = [entry.code for entry in DENIAL_LADDER]
    assert len(set(names)) == len(names) == 15
    assert len(set(codes)) == len(codes)
    assert all(1 <= code <= 255 for code in codes)
    assert len({member.name for member in DenyReason}) == len(DenyReason)
    assert len({int(member) for member in DenyReason}) == len(DenyReason)


def test_priority_and_code_are_separate_and_seven_reasons_differ():
    differing = {entry.name for entry in active_ladder() if entry.priority != entry.code}
    assert differing == {
        "PERSON_CLEAR_DELAY",
        "REQUEST_EXPIRED",
        "ZONE_STORE_INVALID",
        "ELEVATION_LIMIT",
        "IN_ZONE_MARGIN",
        "ON_TIME_LIMIT",
        "COOLDOWN_ACTIVE",
    }


def test_rate_limit_exists_only_as_retired_with_code_13():
    assert DenyReason.RATE_LIMIT == 13
    retired = retired_ladder()
    assert [(entry.name, entry.code, entry.priority) for entry in retired] == [
        ("RATE_LIMIT", 13, None)
    ]
    assert all(entry.name != "RATE_LIMIT" for entry in active_ladder())


def test_no_active_denial_uses_code_13():
    assert all(entry.code != 13 for entry in active_ladder())


def test_next_unused_denial_code_is_16():
    assert next_unused_denial_code() == 16


@given(st.permutations(range(1, 15)))
def test_reordering_priorities_in_a_copy_never_changes_a_code(new_priorities):
    before = {entry.name: entry.code for entry in DENIAL_LADDER}
    shuffled = [
        replace(entry, priority=new_priorities[index])
        for index, entry in enumerate(active_ladder())
    ] + retired_ladder()
    assert {entry.name: entry.code for entry in shuffled} == before
    # The permanent code of each name is still the enum value, whatever the priority.
    assert all(DenyReason[entry.name] == entry.code for entry in shuffled)


def test_original_ladder_is_not_modified_by_the_copy_test():
    assert [entry.priority for entry in active_ladder()] == list(range(1, 15))


# ---- the JSON mirror -------------------------------------------------------------------


def load_json():
    return json.loads(REASON_CODES_FILE.read_text(encoding="utf-8"))


def test_reason_codes_json_equals_the_table_in_codes_py():
    data = load_json()
    from_file = {(r["name"], r["code"], r["status"]) for r in data["reasons"]}
    from_code = {(e.name, e.code, e.status.value) for e in DENIAL_LADDER}
    assert from_file == from_code
    assert len(data["reasons"]) == len(DENIAL_LADDER)


def test_reason_codes_json_has_a_protocol_version_and_retired_entry():
    data = load_json()
    assert data["protocol_version"] == "0.1"
    retired = [r for r in data["reasons"] if r["status"] == "retired"]
    assert retired == [{"name": "RATE_LIMIT", "code": 13, "status": "retired"}]


def test_reason_codes_json_holds_no_priority():
    # Priority is arbitration data only; it must never leak into the permanent table.
    assert all(set(r) == {"name", "code", "status"} for r in load_json()["reasons"])


# ---- reject codes: a separate table ----------------------------------------------------


def test_reject_codes_are_the_documented_values():
    expected = {
        "BAD_FRAMING": 1,
        "BAD_CRC": 2,
        "TRUNCATED": 3,
        "UNKNOWN_TYPE": 4,
        "BAD_LENGTH": 5,
        "STALE_SEQ": 6,
        "FIELD_RANGE": 7,
        "NOT_HOMED": 10,
        "BUSY_HOMING": 11,
        "OUT_OF_SOFT_LIMIT": 12,
        "KEY_OFF": 13,
        "FAULT_ACTIVE": 14,
        "WRONG_STATE": 15,
        "ZONE_NO_UPLOAD": 20,
        "ZONE_BAD_INDEX": 21,
        "ZONE_INCOMPLETE": 22,
        "ZONE_BAD_CHECKSUM": 23,
        "ZONE_TOO_FEW_POINTS": 24,
        "ZONE_TOO_MANY_POINTS": 25,
        "ZONE_SELF_CROSSING": 26,
        "ZONE_DEGENERATE": 27,
        "ZONE_DUPLICATE_ID": 28,
        "ZONE_STORE_FULL": 29,
        "ZONE_UNKNOWN_ID": 30,
        "ZONE_STORAGE_ERROR": 31,
    }
    assert {member.name: int(member) for member in RejectCode} == expected


def test_reject_names_and_codes_are_unique_and_1_to_255():
    codes = [int(member) for member in RejectCode]
    assert len(set(codes)) == len(codes)
    assert all(1 <= code <= 255 for code in codes)
    assert len({member.name for member in RejectCode}) == len(RejectCode)


def test_same_name_in_both_tables_is_allowed_with_different_numbers():
    # KEY_OFF is 5 as a denial reason and 13 as a reject code; FAULT_ACTIVE 2 and 14;
    # NOT_HOMED 4 and 10. Uniqueness is per table.
    assert DenyReason.KEY_OFF == 5
    assert RejectCode.KEY_OFF == 13
    assert DenyReason.FAULT_ACTIVE == 2
    assert RejectCode.FAULT_ACTIVE == 14
    assert DenyReason.NOT_HOMED == 4
    assert RejectCode.NOT_HOMED == 10


def test_retired_code_13_rule_applies_to_the_denial_table_only():
    # Code 13 is retired as a denial code but is a live reject code (KEY_OFF).
    assert RejectCode(13) is RejectCode.KEY_OFF
    assert DenyReason(13) is DenyReason.RATE_LIMIT
    assert all(entry.code != 13 for entry in active_ladder())
    assert 13 in {int(member) for member in RejectCode}


def test_reject_table_is_not_part_of_the_denial_file():
    names = {r["name"] for r in load_json()["reasons"]}
    assert "BAD_CRC" not in names
    assert "ZONE_STORAGE_ERROR" not in names


# ---- states, faults, message types ------------------------------------------------------


def test_exactly_twelve_states_with_codes_0_to_11():
    assert len(State) == 12
    assert [int(member) for member in State] == list(range(12))
    assert [member.name for member in State] == [
        "BOOT",
        "HOMING",
        "DISARMED",
        "SCANNING",
        "CANDIDATE",
        "SLEW",
        "CONFIRM",
        "TRACK",
        "DETER",
        "COOLDOWN",
        "ZONE_SETUP",
        "SAFE_ERROR",
    ]


def test_safe_error_is_displayed_as_safe_slash_error():
    assert STATE_DISPLAY_NAMES[State.SAFE_ERROR] == "SAFE/ERROR"
    assert len(STATE_DISPLAY_NAMES) == 12
    for state in State:
        if state is not State.SAFE_ERROR:
            assert STATE_DISPLAY_NAMES[state] == state.name


def test_fault_bits():
    assert Fault.HEARTBEAT_LOST == 1 << 0
    assert Fault.HOMING_FAILED == 1 << 2
    assert Fault.INTERNAL == 1 << 3
    assert Fault.KEY_OFF == 1 << 4
    assert Fault.UNUSED_BIT_1 == 1 << 1
    assert SETTABLE_FAULTS == Fault.HEARTBEAT_LOST | Fault.KEY_OFF


def test_host_message_types():
    expected = {
        "HOME": 0x01,
        "MOVE": 0x02,
        "JOG": 0x03,
        "HEARTBEAT": 0x04,
        "ARM": 0x05,
        "DISARM": 0x06,
        "ZONE_BEGIN": 0x10,
        "ZONE_POINT": 0x11,
        "ZONE_COMMIT": 0x12,
        "ZONE_DELETE": 0x13,
        "ZONE_LIST": 0x14,
        "ZONE_CLEAR": 0x15,
        "FIRE_REQUEST": 0x20,
        "FIRE_STOP": 0x21,
        "STATUS_REQUEST": 0x30,
    }
    assert {member.name: int(member) for member in MsgType} == expected


def test_reply_type_sets_the_high_bit():
    assert reply_type(MsgType.MOVE) == 0x82
    assert reply_type(MsgType.FIRE_REQUEST) == 0xA0
    assert all(reply_type(member) == member | 0x80 for member in MsgType)
    # No reply type collides with an event, the generic reject or a host type.
    reply_types = {reply_type(member) for member in MsgType}
    assert reply_types.isdisjoint({int(e) for e in EventType} | {GENERIC_REJECT})
    assert reply_types.isdisjoint({int(member) for member in MsgType})


def test_event_types_and_generic_reject():
    assert GENERIC_REJECT == 0xFE
    assert {member.name: int(member) for member in EventType} == {
        "STATUS": 0xC0,
        "REJECT_NOTICE": 0xC1,
        "EFFECTOR_EVENT": 0xC2,
    }


def test_results():
    assert {member.name: int(member) for member in Result} == {
        "OK": 0,
        "REJECTED": 1,
        "GRANTED": 2,
        "DENIED": 3,
    }


def test_effector_events():
    assert {member.name: int(member) for member in EffectorEvent} == {
        "ON": 1,
        "NORMAL_END": 2,
        "CUT": 3,
        "STOPPED_BY_HOST": 4,
    }


@pytest.mark.parametrize("enum", [MsgType, EventType, Result, EffectorEvent, State, RejectCode])
def test_enums_have_unique_values(enum):
    values = [int(member) for member in enum]
    assert len(set(values)) == len(values)
