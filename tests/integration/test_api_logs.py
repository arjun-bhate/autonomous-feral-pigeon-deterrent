"""Tests for GET /api/logs and GET /api/logs/components (T064)."""

import pytest

from deterrent.observability import queries
from support.api_env import api_env


async def engage(env, **body) -> str:
    response = await env.send(
        lambda: env.client.post("/api/dev/engagement", json={"fire": True, **body}), 150
    )
    assert response.status_code == 200
    return response.json()["trace_id"]


async def get(env, path: str, **params):
    env.platform.store.flush()  # the test platform has no background writer thread
    return await env.client.get(path, params=params)


def all_logs(env):
    conn = env.query()
    try:
        return queries.query_logs(conn, limit=1000).items
    finally:
        conn.close()


@pytest.fixture
async def env(tmp_path):
    async with api_env(tmp_path) as e:
        e.trace_id = await engage(e)
        e.platform.logger.warning("api", "a warning from the api")
        e.platform.logger.error("health", "an error from health")
        yield e


async def test_no_filter_returns_the_newest_entries_oldest_first_with_every_field(env):
    response = await get(env, "/api/logs")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"items", "has_more"}
    assert [i["id"] for i in body["items"]] == [r.id for r in all_logs(env)]
    assert set(body["items"][0]) == {
        "id",
        "ts_us",
        "component",
        "severity",
        "message",
        "trace_id",
        "span_id",
        "fields",
    }
    assert body["has_more"] is False


async def test_each_filter_alone_returns_exactly_the_matching_entries(env):
    everything = all_logs(env)
    cases = {
        "component": ("api", lambda r: r.component == "api"),
        "severity": ("ERROR", lambda r: r.severity == "ERROR"),
        "trace_id": (env.trace_id, lambda r: r.trace_id == env.trace_id),
    }
    for name, (value, matches) in cases.items():
        body = (await get(env, "/api/logs", **{name: value})).json()
        assert [i["id"] for i in body["items"]] == [r.id for r in everything if matches(r)], name
        assert body["items"], f"the {name} filter should match something"


async def test_filters_combine_with_and(env):
    body = (await get(env, "/api/logs", component="engagement", trace_id=env.trace_id)).json()
    expected = [
        r.id for r in all_logs(env) if r.component == "engagement" and r.trace_id == env.trace_id
    ]
    assert expected and [i["id"] for i in body["items"]] == expected
    none = (await get(env, "/api/logs", component="health", trace_id=env.trace_id)).json()
    assert none == {"items": [], "has_more": False}
    both = (await get(env, "/api/logs", component="api", severity="WARNING")).json()
    assert [i["message"] for i in both["items"]] == ["a warning from the api"]


async def test_the_engagement_trace_id_returns_its_entries_in_time_order(env):
    items = (await get(env, "/api/logs", trace_id=env.trace_id)).json()["items"]
    assert len(items) >= 3
    assert all(i["trace_id"] == env.trace_id for i in items)
    assert [i["ts_us"] for i in items] == sorted(i["ts_us"] for i in items)


async def test_no_match_is_an_empty_list_with_status_200_not_an_error(env):
    response = await get(env, "/api/logs", component="no-such-component")
    assert response.status_code == 200
    assert response.json() == {"items": [], "has_more": False}
    response = await get(env, "/api/logs", trace_id="0000000000000000")
    assert response.status_code == 200 and response.json()["items"] == []


async def test_the_component_list_is_exactly_the_components_that_logged(env):
    body = (await get(env, "/api/logs/components")).json()
    assert body["items"] == sorted({r.component for r in all_logs(env)})
    assert {"api", "health", "engagement"} <= set(body["items"])


async def test_paging_with_limit_and_before_id_covers_every_entry_once(env):
    everything = [r.id for r in all_logs(env)]
    assert len(everything) > 4
    first = (await get(env, "/api/logs", limit=3)).json()
    assert [i["id"] for i in first["items"]] == everything[-3:] and first["has_more"] is True
    second = (await get(env, "/api/logs", limit=3, before_id=first["items"][0]["id"])).json()
    assert [i["id"] for i in second["items"]] == everything[-6:-3]


async def test_a_limit_above_1000_is_capped_at_1000(env):
    for index in range(1005):
        env.platform.logger.info("bulk", f"line {index}")
    body = (await get(env, "/api/logs", limit=5000)).json()
    assert len(body["items"]) == 1000 and body["has_more"] is True


async def test_a_bad_limit_or_unknown_severity_is_a_clear_422(env):
    for params in ({"limit": 0}, {"limit": "abc"}, {"severity": "LOUD"}, {"before_id": "x"}):
        response = await get(env, "/api/logs", **params)
        assert response.status_code == 422, params
        assert response.json()["error"]["code"] == "INVALID_REQUEST"


async def test_the_logs_endpoints_only_read(env):
    before = all_logs(env)
    await get(env, "/api/logs", component="api")
    await get(env, "/api/logs/components")
    assert len(all_logs(env)) == len(before)
    for path in ("/api/logs", "/api/logs/components"):
        assert (await env.client.post(path)).status_code == 405
