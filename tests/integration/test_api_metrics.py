"""Tests for GET /api/metrics and GET /api/metrics/{name}/series (T068)."""

import math

import pytest

from deterrent.observability.metrics import REQUIRED_METRICS
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


def independent_nearest_rank(values: list[float], percent: int) -> float:
    """Written separately from the application: sort, then take position ceil(p*n/100)."""
    ordered = sorted(values)
    return ordered[math.ceil(percent * len(ordered) / 100) - 1]


def find(items: list[dict], name: str, **labels) -> dict:
    return next(i for i in items if i["name"] == name and i.get("labels", {}) == labels)


SAMPLE_SETS = [
    [5.0],
    [3.0, 1.0],
    [10.0, 20.0, 30.0],
    [float(n) for n in range(1, 11)],
    [float(n) for n in range(1, 21)],  # p95 of 20 is the 19th smallest
    [7.0, 7.0, 7.0, 1.0, 100.0],
    [0.5, 250.25, 3.75, 99.0, 12.5, 12.5, 48.0],
    [float(n % 13) for n in range(1, 101)],
]


@pytest.mark.parametrize("values", SAMPLE_SETS)
async def test_p50_and_p95_equal_an_independent_calculation_on_the_same_samples(tmp_path, values):
    async with api_env(tmp_path, simulator=False) as env:
        for value in values:
            env.platform.metrics.sample("e2e_latency_simulated", value)
        item = find(
            (await get(env, "/api/metrics")).json()["distributions"], "e2e_latency_simulated"
        )
        assert item["count"] == len(values)
        assert item["p50"] == independent_nearest_rank(values, 50)
        assert item["p95"] == independent_nearest_rank(values, 95)
        assert item["method"] == "nearest-rank" and item["unit"] == "milliseconds"


async def test_a_fresh_database_has_no_distributions_not_zeros(tmp_path):
    async with api_env(tmp_path, simulator=False) as env:
        body = (await get(env, "/api/metrics")).json()
        assert body["distributions"] == []
        assert body["counters"] == [] and body["gauges"] == []


async def test_several_engagements_produce_the_counters_and_a_latency_distribution(tmp_path):
    async with api_env(tmp_path) as env:
        await env.arm_and_wait_ready()
        for _ in range(3):
            await engage(env)
            await env.vt.advance(40_000)  # the cooldown after a granted fire has passed
        body = (await get(env, "/api/metrics")).json()
        granted = find(body["counters"], "fire_requests_granted")
        assert granted["total"] == env.counter("fire_requests_granted") >= 1
        latency = find(body["distributions"], "e2e_latency_simulated")
        assert latency["count"] >= 1 and latency["p50"] <= latency["p95"]


async def test_a_denied_request_raises_the_denied_counter_by_exactly_one(tmp_path):
    async with api_env(tmp_path) as env:  # the key is off
        before = (await get(env, "/api/metrics")).json()["counters"]
        count = lambda body: sum(  # noqa: E731
            c["total"]
            for c in body
            if c["name"] == "fire_requests_denied" and c["labels"] == {"reason": "KEY_OFF"}
        )
        await engage(env)
        after = (await get(env, "/api/metrics")).json()["counters"]
        assert count(after) == count(before) + 1


async def test_every_metric_has_a_name_and_a_unit_and_gauges_say_when_they_were_read(tmp_path):
    async with api_env(tmp_path, sensors=True) as env:
        await engage(env)
        await env.vt.advance(12_000)  # gauges are saved every 10 s
        body = (await get(env, "/api/metrics")).json()
        assert body["counters"] and body["gauges"] and body["distributions"]
        for group in body.values():
            for item in group:
                assert item["name"] and item["unit"]
                if item["name"] in REQUIRED_METRICS:
                    assert item["unit"] == REQUIRED_METRICS[item["name"]].unit
        assert all("read_at_us" in g and "value" in g for g in body["gauges"])


async def test_the_series_is_ts_value_pairs_oldest_first(tmp_path):
    async with api_env(tmp_path, simulator=False) as env:
        for value in (1.0, 2.0, 3.0):
            env.platform.metrics.gauge("laptop_cpu_load", value)
            await env.vt.advance(1000)
        body = (await get(env, "/api/metrics/laptop_cpu_load/series")).json()
        assert body["name"] == "laptop_cpu_load" and body["unit"] == "percent"
        assert [p[1] for p in body["points"]] == [1.0, 2.0, 3.0]
        assert all(len(p) == 2 for p in body["points"])
        stamps = [p[0] for p in body["points"]]
        assert stamps == sorted(stamps) and len(set(stamps)) == 3


async def test_the_series_respects_since_us(tmp_path):
    async with api_env(tmp_path, simulator=False) as env:
        for value in (1.0, 2.0, 3.0):
            env.platform.metrics.gauge("laptop_cpu_load", value)
            await env.vt.advance(1000)
        stamps = [
            p[0] for p in (await get(env, "/api/metrics/laptop_cpu_load/series")).json()["points"]
        ]
        body = (await get(env, "/api/metrics/laptop_cpu_load/series", since_us=stamps[1])).json()
        assert [p[1] for p in body["points"]] == [2.0, 3.0]
        late = (
            await get(env, "/api/metrics/laptop_cpu_load/series", since_us=stamps[2] + 1)
        ).json()
        assert late["points"] == []


async def test_a_known_metric_with_no_samples_has_an_empty_series_and_an_unknown_one_is_404(
    tmp_path,
):
    async with api_env(tmp_path, simulator=False) as env:
        empty = await get(env, "/api/metrics/e2e_latency_simulated/series")
        assert empty.status_code == 200
        assert empty.json() == {
            "name": "e2e_latency_simulated",
            "unit": "milliseconds",
            "points": [],
        }
        unknown = await get(env, "/api/metrics/no_such_metric/series")
        assert unknown.status_code == 404
        assert unknown.json()["error"]["code"] == "METRIC_NOT_FOUND"
        bad = await get(env, "/api/metrics/laptop_cpu_load/series", since_us="x")
        assert bad.status_code == 422


async def test_the_metrics_endpoints_only_read(tmp_path):
    async with api_env(tmp_path, simulator=False) as env:
        for path in ("/api/metrics", "/api/metrics/laptop_cpu_load/series"):
            assert (await env.client.post(path)).status_code == 405
