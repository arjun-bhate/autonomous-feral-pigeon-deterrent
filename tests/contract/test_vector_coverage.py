"""Every rule in the protocol must have a vector that names it (FR-041, SC-017).

A rule with no vector is a rule nobody tests. This file reads the protocol document and
the vector files and fails when a rule has no vector whose ``covers`` list names it.

Started in T038 with the FIRE_STOP table; T042 adds the other rules.
"""

import re
from pathlib import Path

from vector_runner import load_vector_files

ROOT = Path(__file__).resolve().parents[2]
DOCUMENT = ROOT / "protocol" / "PROTOCOL.md"

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
