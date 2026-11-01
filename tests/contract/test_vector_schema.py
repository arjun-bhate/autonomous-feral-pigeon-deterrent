"""Tests for the scenario-vector format (T031).

``protocol/vector-schema.json`` is a JSON Schema (a machine-readable description of
which JSON documents are allowed). Every file in ``protocol/vectors/`` must follow it.
"""

import copy
import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "protocol" / "vector-schema.json"
VECTOR_DIR = ROOT / "protocol" / "vectors"
CONTRACT = ROOT / "specs" / "001-platform-core" / "contracts" / "scenario-vectors.md"
FILE_NAMES = ["framing_and_sequence", "fire_arbitration", "zones", "timing"]


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _validator() -> Draft202012Validator:
    return Draft202012Validator(_schema())


def _contract_example() -> dict:
    """The illustrative vector in scenario-vectors.md section 3."""
    text = CONTRACT.read_text(encoding="utf-8")
    section = text[text.index("## 3. Illustrative example") :]
    block = re.search(r"```json\n(.*?)```", section, re.DOTALL)
    assert block is not None
    return json.loads(block.group(1))


def _wrap(*vectors: dict, version: str = "0.1") -> dict:
    return {"protocol_version": version, "vectors": list(vectors)}


GOOD = {
    "id": "x.test.one",
    "description": "A tiny vector.",
    "covers": ["deny:KEY_OFF"],
    "initial": {"state": "SCANNING", "armed": True},
    "steps": [
        {
            "at_ms": 0,
            "action": "send",
            "message": {"type": "ARM", "seq": 1, "fields": {}},
            "expect_reply": {"result": "OK"},
        }
    ],
    "expect": {"state": "SCANNING"},
}


def test_the_schema_itself_is_valid():
    Draft202012Validator.check_schema(_schema())


def test_the_contract_example_validates():
    errors = list(_validator().iter_errors(_wrap(_contract_example())))
    assert errors == []


def test_a_good_vector_validates():
    assert list(_validator().iter_errors(_wrap(GOOD))) == []


def test_every_action_is_accepted():
    vector = copy.deepcopy(GOOD)
    vector["steps"] = [
        {"at_ms": 0, "action": "send", "message": {"type": "HOME", "fields": {}}},
        {"at_ms": 1, "action": "send_raw", "hex": "01 02 00", "expect_reply": "none"},
        {"at_ms": 2, "action": "set_key", "on": True},
        {"at_ms": 3, "action": "inject", "fields": {"zone_store_corrupt": True}},
        {"at_ms": 4, "action": "advance"},
        {"at_ms": 5, "action": "expect", "expect": {"state": "DISARMED"}},
    ]
    assert list(_validator().iter_errors(_wrap(vector))) == []


def test_a_vector_without_id_is_rejected():
    vector = copy.deepcopy(GOOD)
    del vector["id"]
    assert list(_validator().iter_errors(_wrap(vector)))


def test_a_reason_number_instead_of_a_name_is_rejected():
    vector = copy.deepcopy(GOOD)
    vector["steps"][0]["expect_reply"] = {"result": "DENIED", "code": 5}
    assert list(_validator().iter_errors(_wrap(vector)))
    vector["steps"][0]["expect_reply"] = {"result": "DENIED", "code": "KEY_OFF"}
    assert list(_validator().iter_errors(_wrap(vector))) == []


def test_a_cut_cause_given_as_a_number_is_rejected():
    vector = copy.deepcopy(GOOD)
    vector["expect"] = {"events": [{"event": "cut", "cause": 7}]}
    assert list(_validator().iter_errors(_wrap(vector)))


def test_an_unknown_action_is_rejected():
    vector = copy.deepcopy(GOOD)
    vector["steps"][0]["action"] = "teleport"
    assert list(_validator().iter_errors(_wrap(vector)))


def test_an_unknown_message_type_name_is_rejected():
    vector = copy.deepcopy(GOOD)
    vector["steps"][0]["message"]["type"] = "SELF_DESTRUCT"
    assert list(_validator().iter_errors(_wrap(vector)))


def test_unknown_vector_fields_and_initial_fields_are_rejected():
    vector = copy.deepcopy(GOOD)
    vector["colour"] = "red"
    assert list(_validator().iter_errors(_wrap(vector)))
    vector = copy.deepcopy(GOOD)
    vector["initial"]["effector_on"] = True  # the interface must not allow switching it on
    assert list(_validator().iter_errors(_wrap(vector)))


def test_a_file_without_protocol_version_is_rejected():
    assert list(_validator().iter_errors({"vectors": []}))


@pytest.mark.parametrize("name", FILE_NAMES)
def test_each_vector_file_exists_validates_and_records_its_version(name):
    document = json.loads((VECTOR_DIR / f"{name}.json").read_text(encoding="utf-8"))
    assert list(_validator().iter_errors(document)) == []
    reasons = json.loads((ROOT / "protocol" / "reason-codes.json").read_text(encoding="utf-8"))
    assert document["protocol_version"] == reasons["protocol_version"]


def test_vector_ids_are_unique_across_all_files():
    seen: dict[str, str] = {}
    for path in sorted(VECTOR_DIR.glob("*.json")):
        for vector in json.loads(path.read_text(encoding="utf-8"))["vectors"]:
            vector_id = vector["id"]
            assert vector_id not in seen, f"{vector_id} is in {seen[vector_id]} and {path.name}"
            seen[vector_id] = path.name


def test_there_are_exactly_the_four_documented_vector_files():
    assert sorted(p.stem for p in VECTOR_DIR.glob("*.json")) == sorted(FILE_NAMES)
