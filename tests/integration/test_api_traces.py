"""Tests for GET /api/traces and GET /api/traces/{trace_id} (T066)."""

from deterrent.observability.tracing import STEP_NAMES
from support.api_env import api_env


async def engage(env, ms: int = 150, **body) -> str:
    response = await env.send(
        lambda: env.client.post("/api/dev/engagement", json={"fire": True, **body}), ms
    )
    assert response.status_code == 200
    return response.json()["trace_id"]


async def get(env, path: str, **params):
    env.platform.store.flush()  # the test platform has no background writer thread
    return await env.client.get(path, params=params)


def span(trace: dict, name: str) -> dict:
    return next(s for s in trace["spans"] if s["name"] == name)


async def test_a_granted_engagement_has_eight_spans_in_order_and_a_consistent_total(tmp_path):
    async with api_env(tmp_path) as env:
        await env.arm_and_wait_ready()
        trace_id = await engage(env)
        response = await get(env, f"/api/traces/{trace_id}")
        assert response.status_code == 200
        trace = response.json()
        assert trace["trace_id"] == trace_id
        assert trace["outcome"] == "GRANTED" and trace["simulated"] is True
        assert [s["name"] for s in trace["spans"]] == list(STEP_NAMES)
        assert [s["position"] for s in trace["spans"]] == list(range(1, 9))
        last = trace["spans"][-1]
        end = max(s["started_us"] + (s["duration_us"] or 0) for s in trace["spans"])
        assert trace["total_us"] == end - trace["spans"][0]["started_us"]
        assert trace["ended_us"] - trace["started_us"] == trace["total_us"]
        assert last["name"] == "effector_action" and last["status"] == "OK"
        assert set(trace["spans"][0]) == {
            "span_id",
            "position",
            "name",
            "started_us",
            "duration_us",
            "status",
            "simulated",
            "attrs",
        }


async def test_a_denied_engagement_shows_the_reason_and_no_action(tmp_path):
    async with api_env(tmp_path) as env:  # the key is off, so the device denies
        trace = (await get(env, f"/api/traces/{await engage(env)}")).json()
        assert trace["outcome"] == "DENIED" and trace["deny_reason"] == "KEY_OFF"
        ack, action = span(trace, "esp32_ack"), span(trace, "effector_action")
        assert ack["status"] == "DENIED" and ack["attrs"]["reason"] == "KEY_OFF"
        assert action["status"] == "NOT_RUN" and action["attrs"]["note"] == "no action"


async def test_a_dropped_reply_shows_timeout_and_the_step_is_still_present(tmp_path):
    async with api_env(tmp_path, faults=True) as env:
        env.faults.to_host.drop_next()
        trace = (await get(env, f"/api/traces/{await engage(env, ms=700)}")).json()
        assert trace["outcome"] == "TIMEOUT"
        assert len(trace["spans"]) == 8
        assert span(trace, "esp32_ack")["status"] == "TIMEOUT"
        assert span(trace, "effector_action")["status"] == "NOT_RUN"


async def test_an_unknown_trace_id_is_404_trace_not_found(tmp_path):
    async with api_env(tmp_path) as env:
        response = await get(env, "/api/traces/ffffffffffffffff")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "TRACE_NOT_FOUND"


async def test_the_id_from_the_logs_endpoint_opens_the_same_engagement(tmp_path):
    async with api_env(tmp_path) as env:
        trace_id = await engage(env)
        logs = (await get(env, "/api/logs", component="engagement")).json()["items"]
        ids = {entry["trace_id"] for entry in logs if entry["trace_id"]}
        assert ids == {trace_id}
        assert (await get(env, f"/api/traces/{trace_id}")).json()["trace_id"] == trace_id


async def test_the_list_is_newest_first_and_empty_on_a_fresh_database(tmp_path):
    async with api_env(tmp_path) as env:
        assert (await get(env, "/api/traces")).json() == {"items": []}
        first = await engage(env)
        second = await engage(env)
        items = (await get(env, "/api/traces")).json()["items"]
        assert [i["trace_id"] for i in items] == [second, first]
        assert set(items[0]) == {
            "trace_id",
            "started_us",
            "ended_us",
            "total_us",
            "outcome",
            "deny_reason",
            "simulated",
        }
        assert [
            i["trace_id"] for i in (await get(env, "/api/traces", limit=1)).json()["items"]
        ] == [second]


async def test_a_bad_list_limit_is_a_clear_422(tmp_path):
    async with api_env(tmp_path) as env:
        for limit in (0, -1, "x"):
            response = await get(env, "/api/traces", limit=limit)
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "INVALID_REQUEST"


async def test_in_every_one_of_twenty_engagements_the_durations_add_up_to_the_total(tmp_path):
    async with api_env(tmp_path) as env:
        for index in range(20):
            await engage(env, fire=index % 2 == 0)
        items = (await get(env, "/api/traces", limit=100)).json()["items"]
        assert len(items) == 20
        for item in items:
            trace = (await get(env, f"/api/traces/{item['trace_id']}")).json()
            spans = trace["spans"]
            end = max(s["started_us"] + (s["duration_us"] or 0) for s in spans)
            assert trace["total_us"] == end - spans[0]["started_us"]
            # no step starts before the one before it has finished
            for before, after in zip(spans, spans[1:], strict=False):
                assert after["started_us"] >= before["started_us"] + (before["duration_us"] or 0)
            # the steps' own durations never exceed the total
            assert sum(s["duration_us"] or 0 for s in spans) <= trace["total_us"]


async def test_the_trace_endpoints_only_read(tmp_path):
    async with api_env(tmp_path) as env:
        trace_id = await engage(env)
        for path in ("/api/traces", f"/api/traces/{trace_id}"):
            assert (await env.client.post(path)).status_code == 405
