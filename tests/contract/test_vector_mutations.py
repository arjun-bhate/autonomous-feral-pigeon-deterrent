"""Proves that each denial vector fails on one condition only (T035, T036).

The rule for authors is "in each denial vector exactly one condition fails". Here it is
checked mechanically. ``fire.grant.all_conditions_hold`` is a request where every one of
the fourteen conditions holds. ``MUTATIONS`` says, for each denial reason, the one change
that breaks exactly that condition. Three things are then tested:

1. Making that one change to the grant vector and running it gives the matching denial.
2. The denial vector in the file is the grant vector plus exactly that change (nothing
   else differs), so no second condition can be failing by accident.
3. Priority vectors (T036) combine two or more changes on purpose and must be reported
   as the reason with the lower priority number.
"""

import copy

import pytest
from simulator_adapter import SimulatorAdapter
from vector_runner import format_failure, load_reasons, load_vector_files, run_vector

from deterrent.protocol.codes import DENIAL_LADDER

GRANT_ID = "fire.grant.all_conditions_hold"
_ZONE = {"zone_id": 1, "points": [[9500, 2000], [11500, 2000], [11500, 3000], [9500, 3000]]}
_HEAD_AWAY_FROM_ZONE = {"pan_cdeg": 5000, "tilt_cdeg": 3000}
ARMED_INITIAL = {
    "state": "SCANNING",
    "homed": True,
    "armed": True,
    "key_on": True,
    **_HEAD_AWAY_FROM_ZONE,
    "zones": [_ZONE],
}


def grant_vector() -> dict:
    """The vector in which every condition holds (the file must contain exactly this)."""
    return {
        "id": GRANT_ID,
        "description": "Every condition holds, so the fire request is granted.",
        "covers": ["grant:ALL_CONDITIONS"],
        "config": {"elevation_max_cdeg": 4000},
        "initial": copy.deepcopy(ARMED_INITIAL),
        "steps": [
            {
                "at_ms": 0,
                "action": "send",
                "message": {
                    "type": "FIRE_REQUEST",
                    "fields": {"issued_at_ms": 0, "ttl_ms": 500, "duration_ms": 500},
                },
                "expect_reply": {"result": "GRANTED"},
            }
        ],
        "expect": {
            "state": "DETER",
            "effector_on": True,
            "armed": True,
            "fault_mask": [],
            "events": [{"event": "on", "at_ms": 0}],
        },
    }


def _initial(**changes):
    def apply(vector: dict) -> None:
        vector["initial"].update(changes)

    return apply


def _request_arrives_late(vector: dict) -> None:
    vector["steps"][0]["at_ms"] = 501  # issued at 0 with a 500 ms lifetime: now expired


def _request_too_long(vector: dict) -> None:
    vector["steps"][0]["message"]["fields"]["duration_ms"] = 5001


# reason -> (id of its denial vector, the one change that breaks only that condition)
MUTATIONS = {
    "HEARTBEAT_LOST": ("fire.deny.heartbeat_lost", _initial(no_heartbeat=True)),
    "FAULT_ACTIVE": ("fire.deny.fault_active_reserved", _initial(fault_mask=["INTERNAL"])),
    "ZONE_SETUP": ("fire.deny.zone_setup", _initial(state="ZONE_SETUP")),
    "NOT_HOMED": ("fire.deny.not_homed", _initial(homed=False)),
    "KEY_OFF": ("fire.deny.key_off", _initial(key_on=False)),
    "NOT_ARMED": ("fire.deny.not_armed", _initial(armed=False)),
    "PERSON_NOT_CLEAR": ("fire.deny.person_reported", _initial(person_clear=False)),
    "PERSON_CLEAR_DELAY": ("fire.deny.person_clear_delay", _initial(clear_streak_ms=0)),
    "REQUEST_EXPIRED": ("fire.deny.request_expired", _request_arrives_late),
    "ZONE_STORE_INVALID": ("fire.deny.zone_store_invalid", _initial(zone_store_corrupt=True)),
    "ELEVATION_LIMIT": ("fire.deny.elevation_limit", _initial(tilt_cdeg=6000)),
    "IN_ZONE_MARGIN": ("fire.deny.in_zone_margin", _initial(pan_cdeg=10500, tilt_cdeg=2500)),
    "ON_TIME_LIMIT": ("duration.above_per_shot_denied", _request_too_long),
    "COOLDOWN_ACTIVE": (
        "fire.deny.cooldown_active",
        _initial(state="COOLDOWN", cooldown_remaining_ms=2000),
    ),
}
DIFFERS_FROM_PRIORITY = {
    "PERSON_CLEAR_DELAY",
    "REQUEST_EXPIRED",
    "ZONE_STORE_INVALID",
    "ELEVATION_LIMIT",
    "IN_ZONE_MARGIN",
    "ON_TIME_LIMIT",
    "COOLDOWN_ACTIVE",
}


def mutated(*reasons: str) -> dict:
    """The grant vector with the one-condition change of each named reason applied."""
    vector = grant_vector()
    for reason in reasons:
        MUTATIONS[reason][1](vector)
    return vector


def _strip(vector: dict) -> dict:
    """The part of a vector that defines the situation (not its name or what it expects)."""
    plain = copy.deepcopy(vector)
    for name in ("id", "description", "covers", "expect"):
        plain.pop(name, None)
    for step in plain["steps"]:
        step.pop("expect_reply", None)
    return plain


def _vectors() -> dict[str, dict]:
    return {vector["id"]: vector for file, vector in load_vector_files() if file == FILE}


FILE = "fire_arbitration.json"


def _run(vector: dict):
    return run_vector(vector, SimulatorAdapter())


def test_the_grant_vector_in_the_file_is_the_documented_one():
    assert _vectors()[GRANT_ID] == grant_vector()


def test_the_grant_vector_is_granted():
    result = _run(_vectors()[GRANT_ID])
    assert result.passed, format_failure(result)


@pytest.mark.parametrize("reason", sorted(MUTATIONS))
def test_flipping_one_condition_in_the_grant_gives_that_denial(reason):
    vector = mutated(reason)
    vector["id"] = f"mutation.{reason.lower()}"
    vector["steps"][0]["expect_reply"] = {"result": "DENIED", "code": reason}
    vector["expect"] = {"effector_on": False, "events": []}
    result = _run(vector)
    assert result.passed, format_failure(result)


@pytest.mark.parametrize("reason", sorted(MUTATIONS))
def test_the_denial_vector_is_the_grant_plus_exactly_that_change(reason):
    vector_id, _change = MUTATIONS[reason]
    denial = _vectors()[vector_id]
    assert _strip(denial) == _strip(mutated(reason))
    reply = denial["steps"][0]["expect_reply"]
    assert reply["result"] == "DENIED" and reply["code"] == reason
    assert f"deny:{reason}" in denial["covers"]


@pytest.mark.parametrize("reason", sorted(MUTATIONS))
def test_the_denial_vector_passes_and_changes_nothing(reason):
    denial = _vectors()[MUTATIONS[reason][0]]
    result = _run(denial)
    assert result.passed, format_failure(result)
    assert denial["expect"]["effector_on"] is False
    assert denial["expect"]["events"] == []


def test_every_active_reason_has_a_mutation():
    active = {name for name, (_code, status) in load_reasons().items() if status == "active"}
    assert set(MUTATIONS) == active


def test_the_seven_reasons_whose_code_differs_from_priority_are_tagged():
    vectors = _vectors()
    for reason in DIFFERS_FROM_PRIORITY:
        assert "codes:DIFFERS_FROM_PRIORITY" in vectors[MUTATIONS[reason][0]]["covers"], reason
    for reason in set(MUTATIONS) - DIFFERS_FROM_PRIORITY:
        assert "codes:DIFFERS_FROM_PRIORITY" not in vectors[MUTATIONS[reason][0]]["covers"], reason


def test_each_mutation_changes_something_and_the_mutations_do_not_overlap():
    base = _strip(grant_vector())
    seen = []
    for reason in MUTATIONS:
        changed = _strip(mutated(reason))
        assert changed != base, reason
        assert changed not in seen, reason
        seen.append(changed)


# ---- priority vectors (T036): two or more conditions fail on purpose --------

# (vector id, the reason that must be reported, the reason with the higher priority number)
PRIORITY_CASES = [
    ("fire.priority.person_not_clear_over_delay", "PERSON_NOT_CLEAR", "PERSON_CLEAR_DELAY"),
    ("fire.priority.delay_over_expired", "PERSON_CLEAR_DELAY", "REQUEST_EXPIRED"),
    ("fire.priority.key_over_cooldown", "KEY_OFF", "COOLDOWN_ACTIVE"),
    ("fire.priority.delay_over_cooldown", "PERSON_CLEAR_DELAY", "COOLDOWN_ACTIVE"),
    ("fire.priority.on_time_over_cooldown", "ON_TIME_LIMIT", "COOLDOWN_ACTIVE"),
]


def priority_of(reason: str) -> int:
    return next(r.priority for r in DENIAL_LADDER if r.name == reason)


def _expected_denial(vector: dict, reason: str) -> dict:
    vector["steps"][0]["expect_reply"] = {"result": "DENIED", "code": reason}
    vector["expect"] = {"effector_on": False, "events": []}
    return vector


@pytest.mark.parametrize(("vector_id", "winner", "loser"), PRIORITY_CASES)
def test_a_priority_vector_is_two_failing_conditions_and_the_lower_number_wins(
    vector_id, winner, loser
):
    assert priority_of(winner) < priority_of(loser)
    vector = _vectors()[vector_id]
    assert _strip(vector) == _strip(mutated(winner, loser))
    # Each condition really fails here: on its own, in the same situation, it is reported.
    for reason in (winner, loser):
        alone = _expected_denial(mutated(reason), reason)
        result = _run(alone)
        assert result.passed, format_failure(result)
    # Together the reason with the lower priority number is reported (the vector says so).
    assert vector["steps"][0]["expect_reply"] == {"result": "DENIED", "code": winner}
    result = _run(vector)
    assert result.passed, format_failure(result)


@pytest.mark.parametrize(("vector_id", "winner", "loser"), PRIORITY_CASES)
def test_a_priority_vector_names_its_rungs(vector_id, winner, loser):
    tag = f"priority:rung{priority_of(winner)}_over_rung{priority_of(loser)}"
    assert tag in _vectors()[vector_id]["covers"]


def test_the_five_documented_rung_pairs_are_all_present():
    tags = {tag for v in _vectors().values() for tag in v["covers"] if tag.startswith("priority:")}
    assert {
        "priority:rung7_over_rung8",
        "priority:rung8_over_rung9",
        "priority:rung5_over_rung14",
        "priority:rung8_over_rung14",
        "priority:rung13_over_rung14",
    } <= tags


def test_the_retired_reason_is_never_returned_and_there_is_no_per_minute_limit():
    vector = _vectors()["rate.no_per_minute_limit"]
    assert "retired:RATE_LIMIT" in vector["covers"]
    sends = [s for s in vector["steps"] if s["action"] == "send" and "expect_reply" in s]
    assert [s["at_ms"] for s in sends] == [0, 10000, 20000]
    assert all(s["message"]["fields"]["duration_ms"] == 5000 for s in sends)
    assert all(s["expect_reply"] == {"result": "GRANTED"} for s in sends)
    result = _run(vector)
    assert result.passed, format_failure(result)
