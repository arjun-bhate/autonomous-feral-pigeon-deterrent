"""Tests for the simulated engagement (T053), including the REJECTED outcome."""

import asyncio
import re

import pytest

from deterrent.engagement.simulated import SimulatedEngagement, SimulatorNotRunning
from deterrent.link.client import CommandTimeout
from deterrent.observability import queries
from deterrent.observability.tracing import STEP_NAMES
from deterrent.protocol.codes import DenyReason, MsgType, RejectCode
from deterrent.protocol.messages import GenericReject, Reply
from support.platform_env import Env

HEX16 = re.compile(r"^[0-9a-f]{16}$")


@pytest.fixture
async def env(tmp_path):
    e = Env(tmp_path)
    await e.start()
    yield e
    await e.stop()


async def run(env: Env, *, fire: bool = True, duration_ms: int = 500, ms: int = 100) -> str:
    task = asyncio.create_task(env.platform.engagement.run(fire=fire, duration_ms=duration_ms))
    await env.vt.advance(ms)
    return await task


def load_trace(env: Env, trace_id: str):
    conn = env.query()
    try:
        return queries.get_trace(conn, trace_id)
    finally:
        conn.close()


def span(trace, name: str):
    return next(s for s in trace.spans if s.name == name)


# ---- the three device answers ----


async def test_key_off_gives_a_denied_trace_with_no_action(env):
    trace_id = await run(env)
    assert HEX16.match(trace_id)
    trace = load_trace(env, trace_id)
    assert trace.outcome == "DENIED" and trace.deny_reason == "KEY_OFF"
    ack, action = span(trace, "esp32_ack"), span(trace, "effector_action")
    assert ack.status == "DENIED" and ack.attrs["reason"] == "KEY_OFF"
    assert action.status == "NOT_RUN" and action.attrs["note"] == "no action"


async def test_a_fully_armed_device_gives_a_granted_trace(env):
    await env.arm_and_wait_ready()
    trace = load_trace(env, await run(env))
    assert trace.outcome == "GRANTED" and trace.deny_reason is None
    assert span(trace, "esp32_ack").status == "OK"
    action = span(trace, "effector_action")
    assert action.status == "OK" and action.attrs["requested_duration_ms"] == 500


async def test_a_dropped_reply_gives_a_timeout_on_esp32_ack(tmp_path):
    e = Env(tmp_path, faults=True)
    await e.start()
    e.faults.to_host.drop_next()  # the reply to the fire request is lost
    trace = load_trace(e, await run(e, ms=700))
    assert trace.outcome == "TIMEOUT"
    ack, action = span(trace, "esp32_ack"), span(trace, "effector_action")
    assert ack.status == "TIMEOUT" and ack.duration_us == 500_000
    assert action.status == "NOT_RUN"
    await e.stop()


# ---- shape of every trace ----


async def test_every_trace_has_eight_spans_a_consistent_total_and_the_simulated_flag(env):
    for fire in (False, True):
        trace = load_trace(env, await run(env, fire=fire))
        assert [s.name for s in trace.spans] == list(STEP_NAMES)
        assert [s.position for s in trace.spans] == list(range(1, 9))
        assert trace.simulated is True
        assert [s.simulated for s in trace.spans] == [True] * 4 + [False] * 4
        ends = [s.started_us + (s.duration_us or 0) for s in trace.spans]
        assert trace.total_us == max(ends) - trace.spans[0].started_us
        for before, after in zip(trace.spans, trace.spans[1:], strict=False):
            assert after.started_us >= before.started_us + (before.duration_us or 0)


async def test_the_made_up_steps_are_labelled_and_the_rest_are_measured(tmp_path):
    e = Env(tmp_path)
    await e.start()
    trace = load_trace(e, await run(e))
    for name in ("capture", "encode", "network", "inference"):
        assert span(trace, name).simulated is True and span(trace, name).duration_us > 0
    for name in ("decision", "command", "esp32_ack"):
        assert span(trace, name).simulated is False
    await e.stop()


async def test_the_latency_sample_equals_the_trace_total(env):
    trace = load_trace(env, await run(env))
    conn = env.query()
    series = queries.metric_series(conn, "e2e_latency_simulated")
    conn.close()
    assert len(series.points) == 1
    assert series.unit == "milliseconds"
    assert series.points[0][1] == pytest.approx(trace.total_us / 1000)


async def test_no_latency_sample_for_a_timeout_or_when_no_fire_was_requested(tmp_path):
    e = Env(tmp_path, faults=True)
    await e.start()
    await run(e, fire=False)
    e.faults.to_host.drop_next()
    await run(e, ms=700)
    conn = e.query()
    assert queries.metric_series(conn, "e2e_latency_simulated").points == []
    conn.close()
    await e.stop()


async def test_no_fire_requested_sends_nothing_to_the_device(env):
    before = env.platform.client._seq
    trace = load_trace(env, await run(env, fire=False))
    assert trace.outcome == "NO_FIRE_REQUESTED"
    assert [span(trace, n).status for n in ("command", "esp32_ack", "effector_action")] == [
        "NOT_RUN"
    ] * 3
    assert env.platform.client._seq - before <= 6  # only heartbeats were sent meanwhile
    assert env.counter("fire_requests_denied", reason="KEY_OFF") == 0


async def test_log_entries_carry_the_trace_and_span_ids(env):
    trace_id = await run(env)
    trace = load_trace(env, trace_id)
    conn = env.query()
    items = queries.query_logs(conn, trace_id=trace_id).items
    conn.close()
    assert len(items) >= 3
    assert all(i.trace_id == trace_id and i.component == "engagement" for i in items)
    span_ids = {s.span_id for s in trace.spans}
    with_span = [i for i in items if i.span_id]
    assert with_span and all(i.span_id in span_ids for i in with_span)


# ---- REJECTED (owner decision 1) ----


async def test_a_duration_below_the_minimum_gives_a_rejected_trace(env):
    await env.arm_and_wait_ready()
    trace = load_trace(env, await run(env, duration_ms=499))
    assert trace.outcome == "REJECTED" and trace.deny_reason is None
    ack, action = span(trace, "esp32_ack"), span(trace, "effector_action")
    assert ack.status == "REJECTED" and ack.attrs["reject_code"] == "FIELD_RANGE"
    assert action.status == "NOT_RUN" and action.attrs["note"] == "no action"
    assert env.counter("fire_requests_granted") == 0


async def test_zero_duration_is_also_rejected(env):
    trace = load_trace(env, await run(env, duration_ms=0))
    assert trace.outcome == "REJECTED"
    assert span(trace, "esp32_ack").attrs["reject_code"] == "FIELD_RANGE"


class StubClient:
    """A fake client that returns whatever it is told, to prove the engagement never decides."""

    def __init__(self, answer) -> None:
        self.answer = answer
        self.calls = 0

    async def fire_request(self, pan, tilt, duration_ms, ttl_ms=None):
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def make_engagement(env: Env, client) -> SimulatedEngagement:
    p = env.platform
    return SimulatedEngagement(
        lambda: client,
        p.logger,
        p.metrics,
        p.store.submit,
        wall_clock_us=p.wall_clock_us,
        monotonic_us=lambda: env.vt.elapsed_ms * 1000,
    )


async def stub_run(env: Env, client, **kw):
    return load_trace(env, await make_engagement(env, client).run(**kw))


async def test_a_not_homed_rejection_in_boot_is_a_rejected_trace(env):
    stub = StubClient(Reply.rejected(MsgType.FIRE_REQUEST, RejectCode.NOT_HOMED))
    trace = await stub_run(env, stub, fire=True)
    assert trace.outcome == "REJECTED"
    assert span(trace, "esp32_ack").attrs["reject_code"] == "NOT_HOMED"


async def test_a_generic_reject_is_a_rejected_trace(env):
    trace = await stub_run(env, StubClient(GenericReject(0x20)), fire=True)
    assert trace.outcome == "REJECTED"
    assert span(trace, "esp32_ack").attrs["reject_code"] == "UNKNOWN_TYPE"


async def test_an_ok_reply_to_a_fire_request_is_never_treated_as_a_grant(env):
    trace = await stub_run(env, StubClient(Reply.ok(int(MsgType.FIRE_REQUEST))), fire=True)
    assert trace.outcome == "REJECTED"
    assert span(trace, "esp32_ack").attrs["reject_code"] == "UNEXPECTED_REPLY"
    assert span(trace, "effector_action").status == "NOT_RUN"


# ---- the engagement never decides ----


async def test_a_stub_device_that_always_denies_is_reported_as_denied_even_when_armed(env):
    await env.arm_and_wait_ready()  # the real device would grant
    stub = StubClient(Reply.denied(DenyReason.COOLDOWN_ACTIVE))
    trace = await stub_run(env, stub, fire=True)
    assert stub.calls == 1
    assert trace.outcome == "DENIED" and trace.deny_reason == "COOLDOWN_ACTIVE"


async def test_the_grant_comes_only_from_a_granted_reply(env):
    trace = await stub_run(env, StubClient(Reply.granted(1234)), fire=True)
    assert trace.outcome == "GRANTED"
    assert span(trace, "effector_action").attrs["granted_on_ms"] == 1234


async def test_a_timeout_from_the_client_is_a_timeout_trace(env):
    stub = StubClient(CommandTimeout(1, int(MsgType.FIRE_REQUEST)))
    trace = await stub_run(env, stub, fire=True)
    assert trace.outcome == "TIMEOUT"
    assert span(trace, "esp32_ack").status == "TIMEOUT"


async def test_a_missing_simulator_cannot_take_a_fire_engagement(tmp_path):
    e = Env(tmp_path)
    await e.start(simulator=False)
    with pytest.raises(SimulatorNotRunning):
        await e.platform.engagement.run(fire=True)
    trace_id = await e.platform.engagement.run(fire=False)  # no device needed for this one
    assert load_trace(e, trace_id).outcome == "NO_FIRE_REQUESTED"
    await e.stop()


async def test_the_duration_is_passed_to_the_device_unchanged(env):
    stub = StubClient(Reply.denied(DenyReason.KEY_OFF))
    seen = {}

    async def fire_request(pan, tilt, duration_ms, ttl_ms=None):
        seen["duration"] = duration_ms
        return Reply.denied(DenyReason.KEY_OFF)

    stub.fire_request = fire_request
    await stub_run(env, stub, fire=True, duration_ms=4321)
    assert seen["duration"] == 4321


def test_the_documents_list_the_rejected_outcome_and_span_status():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "specs" / "001-platform-core"
    for relative in ("data-model.md", "contracts/dashboard-api.md"):
        text = (root / relative).read_text(encoding="utf-8")
        assert "REJECTED" in text and "reject_code" in text and "no action" in text
