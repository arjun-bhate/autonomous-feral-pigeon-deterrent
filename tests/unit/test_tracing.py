"""Tests for traces and spans (T047)."""

import re

import pytest

from deterrent.observability.store import TraceRecord
from deterrent.observability.tracing import (
    STEP_NAMES,
    Outcome,
    SpanStatus,
    Trace,
)

HEX16 = re.compile(r"^[0-9a-f]{16}$")
HEX8 = re.compile(r"^[0-9a-f]{8}$")


def full_granted_trace(start: int = 1_000_000) -> Trace:
    trace = Trace()
    clock = start
    for name in STEP_NAMES:
        trace.add_span(name, clock, 100, SpanStatus.OK, simulated=name in STEP_NAMES[:4])
        clock += 100
    trace.complete(Outcome.GRANTED)
    return trace


def test_the_eight_fixed_steps_are_in_the_documented_order():
    assert STEP_NAMES == (
        "capture",
        "encode",
        "network",
        "inference",
        "decision",
        "command",
        "esp32_ack",
        "effector_action",
    )


def test_a_completed_trace_has_exactly_eight_spans_in_position_order():
    trace = full_granted_trace()
    assert [s.name for s in trace.spans] == list(STEP_NAMES)
    assert [s.position for s in trace.spans] == [1, 2, 3, 4, 5, 6, 7, 8]


def test_ids_have_the_documented_shape_and_are_unique():
    first, second = Trace(), Trace()
    assert HEX16.match(first.trace_id) and first.trace_id != second.trace_id
    trace = full_granted_trace()
    ids = [s.span_id for s in trace.spans]
    assert all(HEX8.match(i) for i in ids)
    assert len(set(ids)) == 8
    assert all(s.trace_id == trace.trace_id for s in trace.spans)


def test_total_equals_last_end_minus_first_start():
    trace = full_granted_trace(start=5_000)
    assert trace.started_us == 5_000
    assert trace.ended_us == 5_800
    assert trace.total_us == 800


def test_total_includes_gaps_between_spans():
    trace = Trace()
    trace.add_span("capture", 1000, 100, SpanStatus.OK, simulated=True)
    trace.add_span("encode", 1300, 50, SpanStatus.OK, simulated=True)  # a 200 µs gap
    trace.complete(Outcome.NO_FIRE_REQUESTED)
    durations = [s.duration_us or 0 for s in trace.spans]
    assert trace.total_us == 350
    assert trace.total_us == sum(durations) + 200


def test_overlapping_spans_are_refused():
    trace = Trace()
    trace.add_span("capture", 1000, 100, SpanStatus.OK)
    with pytest.raises(ValueError, match="overlap|start"):
        trace.add_span("encode", 1099, 10, SpanStatus.OK)
    assert [s.name for s in trace.spans] == ["capture"]


def test_a_span_may_start_exactly_when_the_previous_one_ends():
    trace = Trace()
    trace.add_span("capture", 1000, 100, SpanStatus.OK)
    trace.add_span("encode", 1100, 10, SpanStatus.OK)
    assert len(trace.spans) == 2


def test_spans_must_be_added_in_step_order_and_only_once():
    trace = Trace()
    trace.add_span("encode", 1000, 10, SpanStatus.OK)
    with pytest.raises(ValueError):
        trace.add_span("capture", 2000, 10, SpanStatus.OK)  # earlier step after a later one
    with pytest.raises(ValueError):
        trace.add_span("encode", 3000, 10, SpanStatus.OK)  # the same step twice
    with pytest.raises(ValueError):
        trace.add_span("teleport", 3000, 10, SpanStatus.OK)  # not one of the eight


def test_a_finished_step_needs_a_duration_and_it_cannot_be_negative():
    trace = Trace()
    with pytest.raises(ValueError):
        trace.add_span("capture", 1000, None, SpanStatus.OK)
    with pytest.raises(ValueError):
        trace.add_span("capture", 1000, -1, SpanStatus.OK)
    with pytest.raises(ValueError):
        trace.add_span("capture", 1000, None, SpanStatus.DENIED)


def test_steps_that_never_happened_are_kept_as_not_run_never_omitted():
    trace = Trace()
    trace.add_span("capture", 1000, 100, SpanStatus.OK, simulated=True)
    trace.add_span("encode", 1100, 100, SpanStatus.OK, simulated=True)
    trace.complete(Outcome.NO_FIRE_REQUESTED)
    assert len(trace.spans) == 8
    later = trace.spans[2:]
    assert all(s.status is SpanStatus.NOT_RUN and s.duration_us is None for s in later)
    assert [s.name for s in trace.spans] == list(STEP_NAMES)
    assert trace.ended_us == 1200  # the placeholders add no time


def test_a_missing_esp32_reply_is_stored_as_timeout():
    trace = Trace()
    for index, name in enumerate(STEP_NAMES[:6]):
        trace.add_span(name, 1000 + index * 100, 100, SpanStatus.OK)
    trace.complete(Outcome.TIMEOUT, unfinished={"esp32_ack": SpanStatus.TIMEOUT})
    ack = trace.spans[6]
    assert (ack.name, ack.status, ack.duration_us) == ("esp32_ack", SpanStatus.TIMEOUT, None)
    assert trace.spans[7].status is SpanStatus.NOT_RUN
    assert trace.outcome is Outcome.TIMEOUT


def test_a_timeout_that_waited_keeps_the_time_it_waited():
    trace = Trace()
    for index, name in enumerate(STEP_NAMES[:6]):
        trace.add_span(name, 1000 + index * 100, 100, SpanStatus.OK)
    trace.add_span("esp32_ack", 1600, 500_000, SpanStatus.TIMEOUT, attrs={"note": "no reply"})
    trace.complete(Outcome.TIMEOUT)
    assert trace.total_us == 501_600 - 1000


def test_a_denied_request_shows_the_reason_and_no_action():
    trace = Trace()
    for index, name in enumerate(STEP_NAMES[:6]):
        trace.add_span(name, 1000 + index * 100, 100, SpanStatus.OK)
    trace.add_span("esp32_ack", 1600, 50, SpanStatus.DENIED, attrs={"reason": "KEY_OFF"})
    trace.complete(Outcome.DENIED, deny_reason="KEY_OFF")
    ack, action = trace.spans[6], trace.spans[7]
    assert ack.status is SpanStatus.DENIED and ack.attrs["reason"] == "KEY_OFF"
    assert action.status is SpanStatus.NOT_RUN and action.attrs["note"] == "no action"
    assert trace.deny_reason == "KEY_OFF"


def test_a_denied_outcome_needs_a_matching_denied_ack():
    trace = Trace()
    for index, name in enumerate(STEP_NAMES[:6]):
        trace.add_span(name, 1000 + index * 100, 100, SpanStatus.OK)
    with pytest.raises(ValueError):
        trace.complete(Outcome.DENIED, deny_reason="KEY_OFF")  # no esp32_ack span at all
    trace.add_span("esp32_ack", 1600, 50, SpanStatus.DENIED, attrs={"reason": "NOT_ARMED"})
    with pytest.raises(ValueError):
        trace.complete(Outcome.DENIED, deny_reason="KEY_OFF")  # reason does not match
    with pytest.raises(ValueError):
        trace.complete(Outcome.DENIED)  # no reason given


def test_only_a_denied_outcome_may_carry_a_deny_reason():
    trace = Trace()
    with pytest.raises(ValueError):
        trace.complete(Outcome.GRANTED, deny_reason="KEY_OFF")


def test_a_running_trace_has_no_end_and_cannot_be_completed_as_running():
    trace = Trace()
    trace.add_span("capture", 1000, 100, SpanStatus.OK)
    assert trace.outcome is Outcome.RUNNING
    assert trace.ended_us is None and trace.total_us is None
    with pytest.raises(ValueError):
        trace.complete(Outcome.RUNNING)


def test_a_completed_trace_cannot_be_changed():
    trace = full_granted_trace()
    with pytest.raises(ValueError):
        trace.add_span("capture", 0, 1, SpanStatus.OK)
    with pytest.raises(ValueError):
        trace.complete(Outcome.GRANTED)


def test_the_simulated_flag_is_kept_on_the_trace_and_each_span():
    trace = full_granted_trace()
    assert trace.simulated is True
    assert [s.simulated for s in trace.spans] == [True] * 4 + [False] * 4


def test_a_span_id_can_be_reserved_before_the_span_is_finished():
    trace = Trace()
    reserved = trace.span_id_for("decision")
    assert trace.span_id_for("decision") == reserved  # the same name gives the same id
    for index, name in enumerate(STEP_NAMES[:4]):
        trace.add_span(name, 1000 + index * 10, 10, SpanStatus.OK)
    span = trace.add_span("decision", 1100, 10, SpanStatus.OK)
    assert span.span_id == reserved


def test_to_record_gives_the_row_the_store_writes():
    trace = full_granted_trace(start=100)
    record = trace.to_record()
    assert isinstance(record, TraceRecord)
    assert (record.trace_id, record.started_us, record.ended_us) == (trace.trace_id, 100, 900)
    assert record.outcome == "GRANTED" and record.deny_reason is None and record.simulated is True
    assert [s.position for s in record.spans] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert record.spans[0].status == "OK" and record.spans[0].simulated is True


def test_to_record_needs_a_completed_trace():
    trace = Trace()
    trace.add_span("capture", 1000, 1, SpanStatus.OK)
    with pytest.raises(ValueError):
        trace.to_record()


def test_outcome_values_are_the_documented_ones():
    assert {o.value for o in Outcome} == {
        "GRANTED",
        "DENIED",
        "TIMEOUT",
        "NO_FIRE_REQUESTED",
        "RUNNING",
        "REJECTED",
    }
    assert {s.value for s in SpanStatus} == {"OK", "DENIED", "TIMEOUT", "NOT_RUN", "REJECTED"}


def trace_up_to_command() -> Trace:
    trace = Trace()
    for index, name in enumerate(STEP_NAMES[:6]):
        trace.add_span(name, 1000 + index * 100, 100, SpanStatus.OK)
    return trace


def test_a_rejected_reply_shows_the_reject_code_and_no_action():
    trace = trace_up_to_command()
    trace.add_span("esp32_ack", 1600, 50, SpanStatus.REJECTED, attrs={"reject_code": "FIELD_RANGE"})
    trace.complete(Outcome.REJECTED)
    ack, action = trace.spans[6], trace.spans[7]
    assert ack.status is SpanStatus.REJECTED and ack.attrs["reject_code"] == "FIELD_RANGE"
    assert action.status is SpanStatus.NOT_RUN and action.attrs["note"] == "no action"
    assert trace.outcome is Outcome.REJECTED and trace.deny_reason is None
    assert trace.to_record().outcome == "REJECTED"


def test_a_rejected_outcome_needs_a_rejected_ack_with_a_reject_code():
    trace = trace_up_to_command()
    with pytest.raises(ValueError):
        trace.complete(Outcome.REJECTED)  # no esp32_ack span at all
    trace.add_span("esp32_ack", 1600, 50, SpanStatus.REJECTED)
    with pytest.raises(ValueError):
        trace.complete(Outcome.REJECTED)  # no reject code on the span


def test_a_rejected_outcome_cannot_carry_a_deny_reason():
    trace = trace_up_to_command()
    trace.add_span("esp32_ack", 1600, 50, SpanStatus.REJECTED, attrs={"reject_code": "NOT_HOMED"})
    with pytest.raises(ValueError):
        trace.complete(Outcome.REJECTED, deny_reason="KEY_OFF")


def test_a_rejected_ack_cannot_end_a_granted_or_denied_trace():
    trace = trace_up_to_command()
    trace.add_span("esp32_ack", 1600, 50, SpanStatus.REJECTED, attrs={"reject_code": "NOT_HOMED"})
    with pytest.raises(ValueError):
        trace.complete(Outcome.GRANTED)
    with pytest.raises(ValueError):
        trace.complete(Outcome.DENIED, deny_reason="NOT_HOMED")
