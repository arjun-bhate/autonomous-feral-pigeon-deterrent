"""Every rule in the protocol must have a vector that names it (FR-041, SC-017).

A rule with no vector is a rule nobody tests. This file reads the protocol document and
the vector files and fails when a rule has no vector whose ``covers`` list names it.

Started in T038 with the FIRE_STOP table. T042 adds every denial reason, zone rejection
code, timing rule, ZONE_SETUP table row, key rule and framing or sequence rule, the
``codes:DIFFERS_FROM_PRIORITY`` tags, and check 5 of the reason-code rules (the numeric code
byte of every DENIED reply is compared with the number in the table).
"""

import json
import re
import shutil
from pathlib import Path

import pytest
from simulator_adapter import SimulatorAdapter
from vector_runner import load_reasons, load_vector_files, run_vector

from deterrent.protocol.codes import DENIAL_LADDER
from deterrent.protocol.framing import build_frame, parse_frame

ROOT = Path(__file__).resolve().parents[2]
DOCUMENT = ROOT / "protocol" / "PROTOCOL.md"
VECTORS = ROOT / "protocol" / "vectors"

# The last row of the FIRE_STOP table covers four states that this feature never enters.
# It says "if the effector is on, behave as DETER", but the effector can only be switched
# on by a real grant, and a grant puts the device in DETER (the test-only interface cannot
# inject DETER, CANDIDATE, SLEW, CONFIRM or TRACK with the effector on). The row is
# therefore unreachable in feature 001 and has no vector. It is exempted here by name, so
# adding a different unexplained row to the table still fails the test.
UNREACHABLE_FIRE_STOP_ROW = "CANDIDATE, SLEW, CONFIRM, TRACK (not entered in this feature)"


def all_covers() -> set[str]:
    return {tag for _file, vector in load_vector_files() for tag in vector["covers"]}


def fire_stop_rows(text: str) -> list[str]:
    """The first cell of every row of the FIRE_STOP-by-state table in section 5.4."""
    start = text.index("Total table of `FIRE_STOP` by state")
    end = text.index("### 5.5 Zones")
    rows = []
    for line in text[start:end].splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if line.startswith("|") and cells[0] not in ("State",) and not set(cells[0]) <= {"-"}:
            rows.append(cells[0])
    return rows


def missing_fire_stop_tags(rows: list[str], covers: set[str]) -> list[str]:
    missing = []
    for row in rows:
        if row == UNREACHABLE_FIRE_STOP_ROW:
            continue
        if not re.fullmatch(r"[A-Z_]+", row):
            missing.append(f"unexplained FIRE_STOP table row: {row!r}")
        elif f"stop:STATE_{row}" not in covers:
            missing.append(f"stop:STATE_{row}")
    return missing


def test_the_fire_stop_table_has_the_expected_rows():
    rows = fire_stop_rows(DOCUMENT.read_text(encoding="utf-8"))
    assert rows == [
        "BOOT",
        "HOMING",
        "DISARMED",
        "SCANNING",
        "DETER",
        "COOLDOWN",
        "ZONE_SETUP",
        "SAFE_ERROR",
        UNREACHABLE_FIRE_STOP_ROW,
    ]


def test_every_fire_stop_state_has_a_vector():
    rows = fire_stop_rows(DOCUMENT.read_text(encoding="utf-8"))
    assert missing_fire_stop_tags(rows, all_covers()) == []


def test_the_running_shot_row_is_covered_by_a_real_grant_not_an_injected_state():
    # DETER cannot be injected; the vector that covers it must start the shot with a request.
    for _file, vector in load_vector_files():
        if "stop:STATE_DETER" in vector["covers"]:
            assert "state" not in vector.get("initial", {}) or vector["initial"]["state"] != "DETER"
            grants = [
                s
                for s in vector["steps"]
                if s["action"] == "send"
                and s["message"]["type"] == "FIRE_REQUEST"
                and s.get("expect_reply", {}).get("result") == "GRANTED"
            ]
            assert grants, vector["id"]


def test_an_unexplained_extra_row_or_a_missing_tag_is_caught():
    rows = fire_stop_rows(DOCUMENT.read_text(encoding="utf-8"))
    assert missing_fire_stop_tags([*rows, "NEW_STATE"], all_covers()) == ["stop:STATE_NEW_STATE"]
    assert missing_fire_stop_tags([*rows, "A, B"], all_covers())
    without_boot = all_covers() - {"stop:STATE_BOOT"}
    assert missing_fire_stop_tags(rows, without_boot) == ["stop:STATE_BOOT"]


# ======================================================================================
# T042: the other rules
# ======================================================================================

# Section 8 of the protocol lists every named value. These are the ones the device enforces
# as a timing rule, so each needs a vector with the tag ``timing:<name>``. The rest are
# listed with the reason they need none, so a new row in section 8 cannot be forgotten.
ENFORCED_TIMING = {
    "heartbeat_interval_ms",
    "watchdog_ms",
    "person_clear_holdoff_ms",
    "person_reengage_delay_ms",
    "fire_ttl_ms",
    "per_shot_ms",
    "cooldown_ms",
    "zone_upload_timeout_ms",
    "homing_duration_ms",
}
NOT_A_DEVICE_TIMING_RULE = {
    "command_ttl_ms": "a host rule: how long the host waits for a reply",
    "aiming_error_cdeg": "an angle added to the zone margin, not a time",
    "max_zones": "a store limit, covered by the ZONE_STORE_FULL vectors",
    "max_points_per_zone": "a store limit, covered by the ZONE_TOO_MANY_POINTS vectors",
    "min duration_ms": "a fixed constant, covered by the duration:RANGE vectors",
}
# Key rules of protocol sections 5.2 and 5.6, one tag each.
KEY_RULES = {
    "key:ANY_STATE": "key off in any state enters SAFE_ERROR (5.2)",
    "key:EDGE_NOT_LEVEL": "the trigger is the change from on to off, not the level (5.2)",
    "key:LIVE_LEVEL": "arbitration reads the live key level (5.2, rung 5)",
    "key:RECOVERY": "SAFE_ERROR is left only when every fault's clearing condition holds (5.6)",
    "key:NEVER_REARMS": "the device never re-arms by itself (5.6)",
    "key:FAULT_PRIORITY": "another fault keeps the device in SAFE_ERROR (5.6)",
}
FRAMING_AND_SEQUENCE_RULES = [
    "framing:BAD_FRAMING",
    "framing:BAD_CRC",
    "framing:TRUNCATED",
    "framing:OVERSIZE",
    "framing:UNKNOWN_TYPE",
    "framing:BAD_LENGTH",
    "framing:FIELD_RANGE",
    "framing:RESYNC",
    "framing:REJECT_NOTICE_RATE",
    "seq:DUPLICATE",
    "seq:DUPLICATE_FIRE",
    "seq:STALE",
    "seq:OUT_OF_ORDER",
    "seq:GAP_ACCEPTED",
    "seq:WRAP",
    "seq:FIRST_ACCEPTED",
    "seq:REJECTED_CACHED",
    "seq:STALE_HEARTBEAT",
    "seq:DUPLICATE_HEARTBEAT",
]
STOP_TAGS = {"RUNNING", "IDLE", "ALL_STATES", "DUPLICATE", "BAD_LENGTH"}
DIFFERS_FROM_PRIORITY = {
    "PERSON_CLEAR_DELAY",
    "REQUEST_EXPIRED",
    "ZONE_STORE_INVALID",
    "ELEVATION_LIMIT",
    "IN_ZONE_MARGIN",
    "ON_TIME_LIMIT",
    "COOLDOWN_ACTIVE",
}


def _table_rows(text: str, start_marker: str, end_marker: str) -> list[list[str]]:
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    rows = []
    for line in text[start:end].splitlines():
        if line.startswith("|"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if not set(cells[0]) <= {"-", ":", " "}:
                rows.append(cells)
    return rows


def zone_rejection_codes(text: str) -> list[str]:
    rows = _table_rows(text, "## 7. Reject codes", "## 8. Timing rules")
    return [r[1] for r in rows if r[0].isdigit() and r[1].startswith("ZONE_")]


def section_8_names(text: str) -> list[str]:
    rows = _table_rows(text, "## 8. Timing rules and defaults", "## 9. Test-only")
    names = []
    for row in rows[1:]:  # the first row is the header
        names += [name.strip().replace("`", "") for name in row[0].split("/")]
    return names


def setup_rows(text: str) -> list[str]:
    rows = _table_rows(text, "#### Leaving ZONE_SETUP", "Events that do **not** leave")
    return [r[0] for r in rows if r[0] == "IN" or r[0].isdigit()]


def required_tags() -> dict[str, str]:
    """Every tag some vector must carry, with why it is required."""
    text = DOCUMENT.read_text(encoding="utf-8")
    required: dict[str, str] = {}
    for name, (_code, status) in load_reasons().items():
        if status == "active":  # a retired reason needs no denial vector
            required[f"deny:{name}"] = "denial reason (section 6)"
    for name in zone_rejection_codes(text):
        required[f"zone:{name}"] = "zone rejection code (section 7)"
    for name in ENFORCED_TIMING:
        required[f"timing:{name}"] = "timing rule (section 8)"
    for row in setup_rows(text):
        required["setup:IN" if row == "IN" else f"setup:EXIT_{row}"] = "ZONE_SETUP table (5.5)"
    required["setup:NONEXIT"] = "ZONE_SETUP list of events that do not leave it (5.5)"
    for tag, why in KEY_RULES.items():
        required[tag] = why
    for tag in FRAMING_AND_SEQUENCE_RULES:
        required[tag] = "framing or sequence rule (sections 2 and 3)"
    required["retired:RATE_LIMIT"] = "the retired reason is never returned (6.1)"
    return required


def missing_coverage(vectors: list[dict]) -> list[str]:
    covers = {tag for vector in vectors for tag in vector["covers"]}
    problems = [
        f"{tag} ({why})" for tag, why in sorted(required_tags().items()) if tag not in covers
    ]
    # the seven reasons whose code differs from their priority must be tagged as such
    for reason in sorted(DIFFERS_FROM_PRIORITY):
        if not any(
            f"deny:{reason}" in v["covers"] and "codes:DIFFERS_FROM_PRIORITY" in v["covers"]
            for v in vectors
        ):
            problems.append(f"codes:DIFFERS_FROM_PRIORITY on a deny:{reason} vector")
    text = DOCUMENT.read_text(encoding="utf-8")
    problems += missing_fire_stop_tags(fire_stop_rows(text), covers)
    return problems


def unknown_tags(vectors: list[dict]) -> list[str]:
    """Tags with a checked prefix whose value does not exist (catches typos)."""
    active = {n for n, (_c, status) in load_reasons().items() if status == "active"}
    text = DOCUMENT.read_text(encoding="utf-8")
    zone_codes = zone_rejection_codes(text)
    allowed = {
        "deny": active,
        "timing": ENFORCED_TIMING,
        "key": {tag.split(":")[1] for tag in KEY_RULES},
        "setup": {"IN", "NONEXIT", *(f"EXIT_{n}" for n in range(1, 9))},
        "stop": STOP_TAGS | {f"STATE_{row}" for row in fire_stop_rows(text) if row.isidentifier()},
    }
    problems = []
    for vector in vectors:
        for tag in vector["covers"]:
            prefix, _, value = tag.partition(":")
            if prefix in allowed and value not in allowed[prefix]:
                problems.append(f"{vector['id']}: unknown tag {tag}")
            if prefix == "zone" and value.startswith("ZONE_") and value not in zone_codes:
                problems.append(f"{vector['id']}: unknown zone code {tag}")
    return problems


_REAL_VECTORS: list[dict] = []


def all_vectors(directory: Path = VECTORS) -> list[dict]:
    """Every vector in the directory. The real set is read once (the tests below do not
    change it); a temporary copy is read fresh each time."""
    if directory != VECTORS:
        return [vector for _file, vector in load_vector_files(directory)]
    if not _REAL_VECTORS:
        _REAL_VECTORS.extend(vector for _file, vector in load_vector_files(directory))
    return list(_REAL_VECTORS)


# ---- the real vector set must be complete -------------------------------------------------------


def test_the_required_tags_are_what_the_protocol_lists():
    required = required_tags()
    assert sum(t.startswith("deny:") for t in required) == 14  # the 14 active reasons
    assert "deny:RATE_LIMIT" not in required  # retired reasons need no denial vector
    assert sum(t.startswith("zone:") for t in required) == 12
    assert sum(t.startswith("setup:") for t in required) == 10  # IN, EXIT_1..8, NONEXIT
    assert sum(t.startswith("timing:") for t in required) == 9
    documented = set(section_8_names(DOCUMENT.read_text(encoding="utf-8")))
    assert documented == ENFORCED_TIMING | set(NOT_A_DEVICE_TIMING_RULE)


def test_every_rule_in_the_protocol_has_a_vector():
    assert missing_coverage(all_vectors()) == []


def test_no_vector_uses_a_misspelt_or_unknown_tag():
    assert unknown_tags(all_vectors()) == []


def test_every_framing_and_sequence_vector_carries_a_framing_or_seq_tag():
    for file, vector in load_vector_files():
        if file == "framing_and_sequence.json":
            assert any(t.startswith(("framing:", "seq:")) for t in vector["covers"]), vector["id"]


def test_the_seven_reasons_whose_code_differs_from_priority_are_the_listed_ones():
    ladder = {r.name: r for r in DENIAL_LADDER}
    differing = {n for n, r in ladder.items() if r.priority and r.priority != r.code}
    assert differing == DIFFERS_FROM_PRIORITY


def test_retired_reasons_need_no_denial_vector_and_no_vector_expects_one():
    assert not any("deny:RATE_LIMIT" in v["covers"] for v in all_vectors())
    for vector in all_vectors():
        for step in vector["steps"]:
            expected = step.get("expect_reply")
            if isinstance(expected, dict):
                assert expected.get("code") != "RATE_LIMIT"


# ---- the check really fails when a vector is missing ---------------------------------------------


def _remove_vectors_carrying(directory: Path, tag: str) -> int:
    removed = 0
    for path in directory.glob("*.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        kept = [v for v in document["vectors"] if tag not in v["covers"]]
        removed += len(document["vectors"]) - len(kept)
        document["vectors"] = kept
        path.write_text(json.dumps(document), encoding="utf-8")
    return removed


def test_a_complete_copy_passes_and_a_copy_with_one_rule_removed_fails(tmp_path):
    copy = tmp_path / "vectors"
    shutil.copytree(VECTORS, copy)
    assert missing_coverage(all_vectors(copy)) == []  # the file complete: passes
    assert _remove_vectors_carrying(copy, "setup:EXIT_7") >= 1
    problems = missing_coverage(all_vectors(copy))
    assert len(problems) == 1 and problems[0].startswith("setup:EXIT_7")


@pytest.mark.parametrize("tag", sorted(required_tags()))
def test_removing_every_vector_for_any_required_tag_is_caught(tag):
    vectors = [v for v in all_vectors() if tag not in v["covers"]]
    assert any(problem.startswith(tag) for problem in missing_coverage(vectors))


def test_removing_the_priority_tag_from_the_vectors_is_caught_for_all_seven_reasons():
    vectors = [
        {**v, "covers": [t for t in v["covers"] if t != "codes:DIFFERS_FROM_PRIORITY"]}
        for v in all_vectors()
    ]
    problems = missing_coverage(vectors)
    assert len([p for p in problems if p.startswith("codes:DIFFERS_FROM_PRIORITY")]) == 7


def test_a_misspelt_tag_is_caught():
    vectors = all_vectors()
    vectors[0] = {**vectors[0], "covers": [*vectors[0]["covers"], "deny:KEY_OF", "stop:STATE_BOT"]}
    assert len(unknown_tags(vectors)) == 2


# ---- check 5: the numeric code byte of every DENIED reply is compared with the table ------


class PriorityOnTheWireAdapter(SimulatorAdapter):
    """Wrong on purpose: puts the reason's priority position, not its permanent code, in the
    code byte of a DENIED reply (the mistake the codes:DIFFERS_FROM_PRIORITY tag exists for)."""

    def send(self, data: bytes):
        return [self._swap(frame) for frame in super().send(data)]

    @staticmethod
    def _swap(frame: bytes) -> bytes:
        parsed = parse_frame(frame[:-1])
        if parsed.type != 0xA0 or parsed.payload[0] != 3:  # not a DENIED reply to FIRE_REQUEST
            return frame
        priority = {r.code: r.priority for r in DENIAL_LADDER if r.priority}[parsed.payload[1]]
        return build_frame(parsed.type, parsed.seq, bytes([3, priority]) + parsed.payload[2:])


@pytest.mark.parametrize("reason", sorted(DIFFERS_FROM_PRIORITY))
def test_a_device_that_sends_the_priority_instead_of_the_code_fails_the_vector(reason):
    vector = next(
        v
        for v in all_vectors()
        if f"deny:{reason}" in v["covers"] and "codes:DIFFERS_FROM_PRIORITY" in v["covers"]
    )
    result = run_vector(vector, PriorityOnTheWireAdapter())
    assert not result.passed, vector["id"]
    assert "code" in result.failure.message


def test_a_reason_whose_code_equals_its_priority_cannot_tell_the_difference():
    vector = next(v for v in all_vectors() if v["id"] == "fire.deny.key_off")  # KEY_OFF is 5 and 5
    assert run_vector(vector, PriorityOnTheWireAdapter()).passed


def test_every_denial_in_every_vector_is_resolved_through_the_table_and_passes():
    reasons = load_reasons()
    checked = 0
    for vector in all_vectors():
        for step in vector["steps"]:
            expected = step.get("expect_reply")
            if isinstance(expected, dict) and expected.get("result") == "DENIED":
                assert expected["code"] in reasons  # resolved through reason-codes.json
                checked += 1
        assert run_vector(vector, SimulatorAdapter()).passed, vector["id"]
    assert checked >= 40
