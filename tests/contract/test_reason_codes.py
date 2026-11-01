"""The permanent denial-reason codes are guarded here (T033).

A denial reason has a *code* (the number on the wire, fixed forever) and a *priority*
(its place in the checking order, free to change). These tests read three things:

* ``protocol/reason-codes.json``: the table the vector runner uses;
* the denial table in ``protocol/PROTOCOL.md`` section 6: what people read;
* ``tests/contract/reason_codes_lock.json``: a locked snapshot of the released codes.

They implement checks 1 to 4, 6 and 7 of ``scenario-vectors.md`` section 8. Check 5
(every active reason has a vector) needs the vectors and lives in
``test_vector_coverage.py``.
"""

import copy
import dataclasses
import json
import re
from pathlib import Path

from deterrent.protocol.codes import DENIAL_LADDER, DenyReason

ROOT = Path(__file__).resolve().parents[2]
TABLE_PATH = ROOT / "protocol" / "reason-codes.json"
LOCK_PATH = Path(__file__).with_name("reason_codes_lock.json")
DOCUMENT_PATH = ROOT / "protocol" / "PROTOCOL.md"

RETIRED_NAME = "RATE_LIMIT"
RETIRED_CODE = 13
# The reasons whose permanent code differs from their checking position.
DIFFERS_FROM_PRIORITY = {
    "PERSON_CLEAR_DELAY",
    "REQUEST_EXPIRED",
    "ZONE_STORE_INVALID",
    "ELEVATION_LIMIT",
    "IN_ZONE_MARGIN",
    "ON_TIME_LIMIT",
    "COOLDOWN_ACTIVE",
}


def load_table() -> dict:
    return json.loads(TABLE_PATH.read_text(encoding="utf-8"))


def load_lock() -> dict:
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


def document_rows(text: str) -> list[dict]:
    """The rows of the denial table in PROTOCOL.md section 6: priority, name, code, status."""
    start = text.index("## 6. Fire denial reasons")
    end = text.index("### 6.1 Reason codes are permanent")
    rows = []
    for line in text[start:end].splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) >= 4 and re.fullmatch(r"[A-Z_]+", cells[1]):
            if cells[2].isdigit():
                rows.append(
                    {
                        "priority": int(cells[0]) if cells[0].isdigit() else None,
                        "name": cells[1],
                        "code": int(cells[2]),
                        "status": cells[3].strip("*"),
                    }
                )
    return rows


# ---- the checks (each returns a list of problems; empty means the check passes) ---------------


def check_unique(table: dict) -> list[str]:
    """Check 1: no two reasons share a code or a name; every code is an integer 1 to 255."""
    problems = []
    names = [r["name"] for r in table["reasons"]]
    codes = [r["code"] for r in table["reasons"]]
    problems += [f"name {n} appears twice" for n in sorted(set(names)) if names.count(n) > 1]
    problems += [
        f"code {c} is used twice" for c in sorted(set(codes), key=str) if codes.count(c) > 1
    ]
    problems += [
        f"code {c!r} is not an integer from 1 to 255"
        for c in codes
        if not isinstance(c, int) or isinstance(c, bool) or not 1 <= c <= 255
    ]
    return problems


def check_document_agrees(table: dict, rows: list[dict]) -> list[str]:
    """Check 2: the document and the file list exactly the same name, code and status."""
    from_file = {(r["name"], r["code"], r["status"]) for r in table["reasons"]}
    from_document = {(r["name"], r["code"], r["status"]) for r in rows}
    problems = [f"only in the file: {t}" for t in sorted(from_file - from_document)]
    problems += [f"only in the document: {t}" for t in sorted(from_document - from_file)]
    return problems


def check_unchanged(table: dict, lock: dict) -> list[str]:
    """Check 3: every locked pair is still there, a retired one stays retired, and a new
    reason takes the next unused code above every locked code."""
    problems = []
    by_name = {r["name"]: r for r in table["reasons"]}
    for locked in lock["reasons"]:
        current = by_name.get(locked["name"])
        if current is None:
            problems.append(f"{locked['name']} ({locked['code']}) was removed from the table")
            continue
        if current["code"] != locked["code"]:
            problems.append(
                f"{locked['name']} was code {locked['code']} and is now {current['code']}"
            )
        if locked["status"] == "retired" and current["status"] != "retired":
            problems.append(f"{locked['name']} was retired and is marked {current['status']}")
    locked_names = {r["name"] for r in lock["reasons"]}
    top = max(r["code"] for r in lock["reasons"])
    new_codes = sorted(r["code"] for r in table["reasons"] if r["name"] not in locked_names)
    expected = list(range(top + 1, top + 1 + len(new_codes)))
    if new_codes != expected:
        problems.append(f"new reasons must take the next unused codes {expected}, not {new_codes}")
    return problems


def check_retired_code(table: dict) -> list[str]:
    """Check 7 (table half): code 13 belongs to RATE_LIMIT alone and RATE_LIMIT is retired."""
    problems = []
    for reason in table["reasons"]:
        if reason["code"] == RETIRED_CODE and reason["name"] != RETIRED_NAME:
            problems.append(f"code 13 is given to {reason['name']}")
        if reason["name"] == RETIRED_NAME and reason["status"] != "retired":
            problems.append("RATE_LIMIT is not marked retired")
        if reason["name"] == RETIRED_NAME and reason["code"] != RETIRED_CODE:
            problems.append(f"RATE_LIMIT has code {reason['code']}, not 13")
    if not any(r["name"] == RETIRED_NAME for r in table["reasons"]):
        problems.append("the retired RATE_LIMIT pair is missing")
    return problems


def check_priority_is_not_code(rows: list[dict]) -> list[str]:
    """Check 6 (table half): for the seven listed reasons the code differs from the priority."""
    problems = []
    by_name = {r["name"]: r for r in rows}
    for name in sorted(DIFFERS_FROM_PRIORITY):
        row = by_name.get(name)
        if row is None or row["priority"] == row["code"]:
            problems.append(f"{name}: its code must differ from its priority")
    return problems


# ---- the real files must pass every check ------------------------------------------------------


def test_codes_and_names_are_unique():
    assert check_unique(load_table()) == []


def test_document_and_file_list_the_same_triples():
    rows = document_rows(DOCUMENT_PATH.read_text(encoding="utf-8"))
    assert len(rows) == 15
    assert check_document_agrees(load_table(), rows) == []


def test_released_codes_are_unchanged():
    assert check_unchanged(load_table(), load_lock()) == []


def test_the_lock_matches_the_code_in_the_program():
    in_program = {r.name: (r.code, r.status.value) for r in DENIAL_LADDER}
    in_lock = {r["name"]: (r["code"], r["status"]) for r in load_lock()["reasons"]}
    assert in_program == in_lock
    assert {r.name: int(r) for r in DenyReason} == {n: c for n, (c, _s) in in_lock.items()}


def test_the_retired_code_is_still_retired():
    assert check_retired_code(load_table()) == []


def test_codes_that_differ_from_priority_do_differ():
    rows = document_rows(DOCUMENT_PATH.read_text(encoding="utf-8"))
    assert check_priority_is_not_code(rows) == []


def test_lock_and_table_record_the_same_protocol_version():
    assert load_lock()["protocol_version"] == load_table()["protocol_version"]


# ---- negative tests on copies: each broken table must be caught ----------------------------------


def _broken(change) -> dict:
    table = copy.deepcopy(load_table())
    change(table["reasons"])
    return table


def _reason(reasons: list[dict], name: str) -> dict:
    return next(r for r in reasons if r["name"] == name)


def test_a_reassigned_code_fails():
    table = _broken(lambda rs: _reason(rs, "KEY_OFF").update(code=99))
    assert any("KEY_OFF" in p for p in check_unchanged(table, load_lock()))


def test_a_removed_retired_pair_fails():
    table = _broken(lambda rs: rs.remove(_reason(rs, "RATE_LIMIT")))
    assert any("RATE_LIMIT" in p for p in check_unchanged(table, load_lock()))
    assert check_retired_code(table)


def test_rate_limit_marked_active_fails():
    table = _broken(lambda rs: _reason(rs, "RATE_LIMIT").update(status="active"))
    assert any("retired" in p for p in check_unchanged(table, load_lock()))
    assert check_retired_code(table)


def test_code_13_given_to_another_name_fails():
    table = _broken(lambda rs: rs.append({"name": "BRAND_NEW", "code": 13, "status": "active"}))
    assert any("code 13" in p for p in check_retired_code(table))
    assert check_unique(table)  # 13 is now used twice


def test_a_new_reason_with_code_16_passes():
    table = _broken(lambda rs: rs.append({"name": "BRAND_NEW", "code": 16, "status": "active"}))
    assert check_unique(table) == []
    assert check_unchanged(table, load_lock()) == []
    assert check_retired_code(table) == []


def test_a_new_reason_with_code_14_fails():
    table = _broken(lambda rs: rs.append({"name": "BRAND_NEW", "code": 14, "status": "active"}))
    assert check_unique(table)
    assert check_unchanged(table, load_lock())


def test_a_new_reason_that_skips_a_number_fails():
    table = _broken(lambda rs: rs.append({"name": "BRAND_NEW", "code": 17, "status": "active"}))
    assert check_unchanged(table, load_lock())


def test_duplicate_names_and_bad_numbers_fail():
    twice = _broken(lambda rs: rs.append({"name": "KEY_OFF", "code": 16, "status": "active"}))
    assert any("KEY_OFF" in p for p in check_unique(twice))
    for bad in (0, 256, "5", True):
        table = _broken(lambda rs, bad=bad: _reason(rs, "NOT_HOMED").update(code=bad))
        assert check_unique(table), bad


def test_reordering_priorities_leaves_every_code_unchanged():
    before = {r.name: r.code for r in DENIAL_LADDER}
    active = [r for r in DENIAL_LADDER if r.priority is not None]
    reversed_ladder = [
        dataclasses.replace(r, priority=len(active) - r.priority + 1) for r in active
    ]
    assert [r.priority for r in reversed_ladder] != [r.priority for r in active]
    after = {r.name: r.code for r in reversed_ladder}
    assert after == {name: code for name, code in before.items() if name in after}
    locked = {r["name"]: r["code"] for r in load_lock()["reasons"]}
    assert all(locked[name] == code for name, code in after.items())


def test_a_document_that_disagrees_with_the_file_fails():
    text = DOCUMENT_PATH.read_text(encoding="utf-8")
    changed = text.replace("| 5 | KEY_OFF | 5 | active |", "| 5 | KEY_OFF | 50 | active |")
    assert changed != text
    assert check_document_agrees(load_table(), document_rows(changed))


def test_a_document_row_given_the_wrong_status_fails():
    text = DOCUMENT_PATH.read_text(encoding="utf-8")
    changed = text.replace(
        "| none | RATE_LIMIT | 13 | **retired** |", "| none | RATE_LIMIT | 13 | active |"
    )
    assert changed != text
    assert check_document_agrees(load_table(), document_rows(changed))


def test_a_code_equal_to_its_priority_fails_check_6():
    rows = document_rows(DOCUMENT_PATH.read_text(encoding="utf-8"))
    for row in rows:
        if row["name"] == "COOLDOWN_ACTIVE":
            row["code"] = row["priority"]
    assert check_priority_is_not_code(rows)
