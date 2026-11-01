"""Tests for the vector runner itself (T032): can it pass, can it fail, does it refuse bad vectors.

A runner that cannot fail proves nothing, so most tests here run a vector against a
deliberately wrong adapter and check that the runner notices.
"""

import copy
from dataclasses import replace

import pytest
from simulator_adapter import SimulatorAdapter
from vector_runner import InvalidVector, format_failure, plan_steps, run_vector

from deterrent.protocol.codes import State
from deterrent.protocol.framing import build_frame, parse_frame

ARMED = {"state": "SCANNING", "homed": True, "armed": True, "key_on": True}


def fire(at_ms: int, **extra) -> dict:
    step = {
        "at_ms": at_ms,
        "action": "send",
        "message": {"type": "FIRE_REQUEST", "fields": {"duration_ms": 500}},
    }
    step.update(extra)
    return step


TINY = {
    "id": "runner.tiny.key_off",
    "description": "Only the key is off, so the request is denied KEY_OFF.",
    "covers": ["deny:KEY_OFF"],
    "initial": {**ARMED, "key_on": False},
    "heartbeats": [{"from_ms": 0, "until_ms": 100}],
    "steps": [fire(0, expect_reply={"result": "DENIED", "code": "KEY_OFF"})],
    "expect": {"effector_on": False, "state": "SCANNING", "fault_mask": []},
}

GRANT = {
    "id": "runner.tiny.grant",
    "description": "Everything holds, so the request is granted.",
    "covers": ["timing:per_shot_ms"],
    "initial": ARMED,
    "heartbeats": [{"from_ms": 0, "until_ms": 1000}],
    "steps": [
        fire(0, expect_reply={"result": "GRANTED"}),
        {"at_ms": 499, "action": "expect", "expect": {"state": "DETER", "effector_on": True}},
        {"at_ms": 500, "action": "expect", "expect": {"state": "COOLDOWN", "effector_on": False}},
    ],
    "expect": {"events": [{"event": "on", "at_ms": 0}, {"event": "normal_end", "at_ms": 500}]},
}


def run(vector: dict, adapter=None):
    return run_vector(vector, adapter if adapter is not None else SimulatorAdapter())


# ---- a vector can pass --------------------------------------------------------------------


def test_a_tiny_vector_passes_against_the_simulator():
    result = run(TINY)
    assert result.passed, format_failure(result)


def test_a_grant_with_timed_expectations_passes():
    result = run(GRANT)
    assert result.passed, format_failure(result)


# ---- and it can fail ----------------------------------------------------------------------


class AlwaysScanningAdapter(SimulatorAdapter):
    """Wrong on purpose: reports the state SCANNING whatever the device is doing."""

    def read_status(self):
        return replace(super().read_status(), state=State.SCANNING)


class DeafAdapter(SimulatorAdapter):
    """Wrong on purpose: never returns any reply."""

    def send(self, data: bytes):
        super().send(data)
        return []


def _request_seq(data: bytes) -> int:
    frame = parse_frame(data[:-1])
    return frame.seq


class AlwaysGrantAdapter(SimulatorAdapter):
    """Wrong on purpose: answers every fire request with the bytes of a grant."""

    def send(self, data: bytes):
        frames = super().send(data)
        if parse_frame(data[:-1]).type == 0x20:
            return [build_frame(0xA0, _request_seq(data), bytes([2, 0]) + bytes(4))]
        return frames


def test_the_same_vector_fails_against_a_wrong_adapter():
    assert run(GRANT).passed
    result = run(GRANT, AlwaysScanningAdapter())
    assert not result.passed
    assert "state" in result.failure.message


def test_a_missing_reply_is_a_failure():
    result = run(TINY, DeafAdapter())
    assert not result.passed
    assert "no reply" in result.failure.message


def test_a_wrong_reply_is_a_failure():
    result = run(TINY, AlwaysGrantAdapter())
    assert not result.passed


def test_failure_output_names_the_vector_and_the_first_mismatch():
    vector = copy.deepcopy(TINY)
    vector["steps"][0]["expect_reply"]["code"] = "NOT_ARMED"
    result = run(vector)
    assert not result.passed
    text = format_failure(result)
    assert "runner.tiny.key_off" in text
    assert "KEY_OFF" in text and "NOT_ARMED" in text  # what came back and what was expected
    assert "at 0 ms" in text


def test_only_the_first_mismatch_is_reported():
    vector = copy.deepcopy(TINY)
    vector["steps"][0]["expect_reply"]["code"] = "NOT_ARMED"
    vector["expect"]["state"] = "DISARMED"
    result = run(vector)
    assert "NOT_ARMED" in result.failure.message  # the reply is wrong before the final state is


def test_an_event_that_is_missing_or_extra_is_a_failure():
    vector = copy.deepcopy(GRANT)
    vector["expect"] = {"events": [{"event": "on"}]}
    assert not run(vector).passed  # the normal end happened too
    vector["expect"] = {"events": [{"event": "on"}, {"event": "normal_end"}, {"event": "cut"}]}
    assert not run(vector).passed


def test_within_ms_is_measured_from_the_last_explicit_step():
    vector = copy.deepcopy(GRANT)
    vector["steps"] = vector["steps"][:1] + [
        {"at_ms": 500, "action": "advance"},
    ]
    vector["expect"] = {"events": [{"event": "on", "within_ms": 100}, {"event": "normal_end"}]}
    assert run(vector).passed
    vector["expect"] = {"events": [{"event": "on"}, {"event": "normal_end", "within_ms": 100}]}
    assert not run(vector).passed  # the end came 500 ms after the last explicit step


def test_a_counter_is_checked_when_the_device_can_report_it():
    vector = copy.deepcopy(TINY)
    vector["expect"] = {"counters": {"interlock.total": 0, "fire_denied.KEY_OFF": 1}}
    assert run(vector).passed
    vector["expect"] = {"counters": {"fire_denied.KEY_OFF": 2}}
    assert not run(vector).passed


# ---- retired code 13 ------------------------------------------------------------------------


class RetiredReplyAdapter(SimulatorAdapter):
    """Wrong on purpose: replies DENIED with the retired code 13."""

    def send(self, data: bytes):
        frames = super().send(data)
        if parse_frame(data[:-1]).type == 0x20:
            return [build_frame(0xA0, _request_seq(data), bytes([3, 13, 0, 0]))]
        return frames


class RetiredCutAdapter(SimulatorAdapter):
    """Wrong on purpose: reports an effector cut whose cause is the retired code 13."""

    def send(self, data: bytes):
        return [*super().send(data), build_frame(0xC2, 0, bytes([3, 13]))]


def test_retired_code_13_in_a_reply_fails_the_run():
    vector = copy.deepcopy(TINY)
    del vector["steps"][0]["expect_reply"]  # the vector does not even mention the reply
    result = run(vector, RetiredReplyAdapter())
    assert not result.passed
    assert "13" in result.failure.message and "retired" in result.failure.message


def test_retired_code_13_as_a_cut_cause_fails_the_run():
    vector = copy.deepcopy(TINY)
    del vector["steps"][0]["expect_reply"]
    result = run(vector, RetiredCutAdapter())
    assert not result.passed
    assert "retired" in result.failure.message


# ---- bad vectors are refused -----------------------------------------------------------------


def test_a_vector_writing_a_reason_number_is_refused():
    vector = copy.deepcopy(TINY)
    vector["steps"][0]["expect_reply"]["code"] = 5
    with pytest.raises(InvalidVector):
        run(vector)


def test_an_unknown_reason_name_is_refused():
    vector = copy.deepcopy(TINY)
    vector["steps"][0]["expect_reply"]["code"] = "NO_SUCH_REASON"
    with pytest.raises(InvalidVector, match="NO_SUCH_REASON"):
        run(vector)


def test_the_retired_reason_cannot_be_expected():
    vector = copy.deepcopy(TINY)
    vector["steps"][0]["expect_reply"]["code"] = "RATE_LIMIT"
    with pytest.raises(InvalidVector, match="retired"):
        run(vector)


def test_an_unknown_message_field_is_refused():
    vector = copy.deepcopy(TINY)
    vector["steps"][0]["message"]["fields"]["colour"] = 1
    with pytest.raises(InvalidVector, match="colour"):
        run(vector)


def test_steps_out_of_time_order_are_refused():
    vector = copy.deepcopy(GRANT)
    vector["steps"] = [fire(500), fire(100)]
    with pytest.raises(InvalidVector, match="order"):
        run(vector)


# ---- how the runner builds and orders what it sends ---------------------------------------------


def test_a_scheduled_heartbeat_goes_before_a_step_at_the_same_instant():
    vector = {
        "id": "runner.order.heartbeat_first",
        "description": "Only the plan matters here.",
        "covers": ["person:BOUNDARY"],
        "heartbeats": [{"from_ms": 0, "until_ms": 300, "every_ms": 100}],
        "steps": [fire(100), fire(300)],
    }
    plan = plan_steps(vector)
    assert [(s["at_ms"], s["message"]["type"]) for s in plan] == [
        (0, "HEARTBEAT"),
        (100, "HEARTBEAT"),
        (100, "FIRE_REQUEST"),
        (200, "HEARTBEAT"),
        (300, "HEARTBEAT"),
        (300, "FIRE_REQUEST"),
    ]


def test_a_request_exactly_at_the_reengage_delay_sees_the_heartbeat_of_that_instant():
    vector = {
        "id": "runner.order.delay_boundary",
        "description": "A request exactly at the re-engage delay is granted.",
        "covers": ["person:BOUNDARY"],
        "config": {"person_reengage_delay_ms": 300},
        "initial": {**ARMED, "person_clear": False},
        "heartbeats": [{"from_ms": 0, "until_ms": 400, "every_ms": 100}],
        "steps": [
            fire(299, expect_reply={"result": "DENIED", "code": "PERSON_CLEAR_DELAY"}),
            fire(300, expect_reply={"result": "GRANTED"}),
        ],
    }
    result = run(vector)
    assert result.passed, format_failure(result)


def test_messages_are_numbered_automatically_and_explicit_numbers_are_kept():
    vector = {
        "id": "runner.seq.explicit",
        "description": "A repeated explicit number is a duplicate: same reply, one action.",
        "covers": ["seq:DUPLICATE"],
        "initial": ARMED,
        "heartbeats": [{"from_ms": 0, "until_ms": 100, "every_ms": 50}],
        "steps": [
            {
                "at_ms": 10,
                "action": "send",
                "message": {
                    "type": "MOVE",
                    "seq": 40,
                    "fields": {"pan_cdeg": 100, "tilt_cdeg": 50},
                },
                "expect_reply": {"result": "OK", "data": {"pan": 100, "tilt": 50}},
            },
            {
                "at_ms": 20,
                "action": "send",
                "message": {
                    "type": "MOVE",
                    "seq": 40,
                    "fields": {"pan_cdeg": 900, "tilt_cdeg": 50},
                },
                "expect_reply": {"result": "OK", "data": {"pan": 100, "tilt": 50}},
            },
        ],
        "expect": {"pan_cdeg": 100},
    }
    result = run(vector)
    assert result.passed, format_failure(result)


def _corrupted_arm_frame_hex() -> str:
    wire = bytearray(build_frame(0x05, 3))
    index = len(wire) - 2  # the last byte before the 0x00 delimiter (part of the CRC)
    wire[index] ^= 0x01
    assert 0 not in wire[:-1]
    return bytes(wire).hex(" ").upper()


def test_send_raw_with_none_expected_passes_when_the_device_stays_silent():
    vector = {
        "id": "runner.raw.silent",
        "description": "A frame with a broken checksum gets no reply.",
        "covers": ["framing:BAD_CRC"],
        "initial": ARMED,
        "steps": [
            {
                "at_ms": 0,
                "action": "send_raw",
                "hex": _corrupted_arm_frame_hex(),
                "expect_reply": "none",
            }
        ],
        "expect": {"counters": {"rejected.BAD_CRC": 1, "wire.reject_notice": 1}},
    }
    result = run(vector)
    assert result.passed, format_failure(result)
