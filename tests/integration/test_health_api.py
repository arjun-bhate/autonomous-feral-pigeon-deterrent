"""Tests for the health data in the API and the live channel (T059)."""

import asyncio

import pytest

from deterrent.health.sensors import SENSOR_UNITS
from deterrent.observability import queries
from support.api_env import api_env
from support.fake_socket import FakeSocket

NAMES = list(SENSOR_UNITS)


async def get_readings(env) -> dict[str, dict]:
    data = (await env.client.get("/api/health/current")).json()
    return {r["name"]: r for r in data["readings"]}


async def post(env, path: str, body: dict, ms: int = 60):
    return await env.send(lambda: env.client.post(path, json=body), ms)


async def connect(env) -> FakeSocket:
    socket = FakeSocket()
    asyncio.create_task(env.app.state.hub.serve(socket))
    await env.vt.advance(1)
    return socket


# ---- GET /api/health/current ----


async def test_current_readings_have_the_documented_shape(tmp_path):
    async with api_env(tmp_path, simulator=False, sensors=True) as env:
        await env.vt.advance(1500)
        body = (await env.client.get("/api/health/current")).json()
        assert list(body) == ["readings"]
        assert [r["name"] for r in body["readings"]] == NAMES
        for reading in body["readings"]:
            assert set(reading) == {"name", "value", "unit", "state", "read_at_us"}
            assert reading["unit"] == SENSOR_UNITS[reading["name"]]
            assert reading["state"] == "available"
            assert isinstance(reading["value"], float)
            assert reading["read_at_us"] > 0
        assert body["readings"][0]["value"] == 10.0


async def test_the_readings_exist_even_before_the_first_read(tmp_path):
    async with api_env(tmp_path, simulator=False, sensors=True) as env:
        readings = (await get_readings(env)).values()
        assert len(readings) == 5
        assert all(r["state"] in ("available", "unavailable") for r in readings)


async def test_with_no_sensor_at_all_the_list_is_empty(tmp_path):
    async with api_env(tmp_path, simulator=False) as env:
        assert (await env.client.get("/api/health/current")).json() == {"readings": []}


# ---- failing and restoring one sensor ----


async def test_failing_the_gpu_sensor_leaves_the_others_live(tmp_path):
    async with api_env(tmp_path, simulator=False, sensors=True) as env:
        await env.vt.advance(1500)
        response = await post(env, "/api/dev/sensors/gpu_load/fail", {"on": True})
        assert response.status_code == 200
        assert response.json() == {"name": "gpu_load", "failed": True, "mode": "error"}
        await env.vt.advance(2500)
        readings = await get_readings(env)
        assert readings["gpu_load"]["state"] == "unavailable"
        assert readings["gpu_load"]["value"] is None
        for name in NAMES:
            if name != "gpu_load":
                assert readings[name]["state"] == "available"
                assert readings[name]["value"] is not None


async def test_a_hanging_sensor_is_unavailable_within_a_couple_of_seconds(tmp_path):
    async with api_env(tmp_path, simulator=False, sensors=True) as env:
        await env.vt.advance(1500)
        await post(env, "/api/dev/sensors/cpu_temp/fail", {"on": True, "mode": "hang"})
        await env.vt.advance(2500)
        readings = await get_readings(env)
        assert readings["cpu_temp"]["state"] == "unavailable"
        assert readings["cpu_load"]["state"] == "available"
        await post(env, "/api/dev/sensors/cpu_temp/fail", {"on": False})
        await env.vt.advance(3000)
        assert (await get_readings(env))["cpu_temp"]["state"] == "available"


async def test_unfailing_restores_the_value(tmp_path):
    async with api_env(tmp_path, simulator=False, sensors=True) as env:
        await post(env, "/api/dev/sensors/gpu_load/fail", {"on": True})
        await env.vt.advance(2500)
        assert (await get_readings(env))["gpu_load"]["value"] is None
        response = await post(env, "/api/dev/sensors/gpu_load/fail", {"on": False})
        assert response.json() == {"name": "gpu_load", "failed": False, "mode": "error"}
        await env.vt.advance(2500)
        assert (await get_readings(env))["gpu_load"]["value"] == 11.0


async def test_the_sensor_route_checks_name_body_and_dev_tools(tmp_path):
    async with api_env(tmp_path, simulator=False, sensors=True) as env:
        unknown = await post(env, "/api/dev/sensors/fan_speed/fail", {"on": True})
        assert unknown.status_code == 404
        assert unknown.json()["error"]["code"] == "SENSOR_NOT_FOUND"
        extra = await post(env, "/api/dev/sensors/gpu_load/fail", {"on": True, "grant": 1})
        assert extra.status_code == 422
        bad_mode = await post(env, "/api/dev/sensors/gpu_load/fail", {"on": True, "mode": "melt"})
        assert bad_mode.status_code == 422
    async with api_env(tmp_path / "off", simulator=False, sensors=True, dev_tools=False) as env:
        assert (await post(env, "/api/dev/sensors/gpu_load/fail", {"on": True})).status_code == 404


# ---- the live channel ----


async def test_health_messages_arrive_at_least_every_two_seconds(tmp_path):
    async with api_env(tmp_path, simulator=False, sensors=True) as env:
        socket = await connect(env)
        times = []
        for _ in range(100):  # ten seconds in tenth-of-a-second steps
            before = len(socket.of_type("health"))
            await env.vt.advance(100)
            if len(socket.of_type("health")) > before:
                times.append(env.vt.elapsed_ms)
        assert len(times) >= 5
        gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
        assert max(gaps) <= 2000


async def test_a_health_message_carries_the_same_readings_as_the_rest_api(tmp_path):
    async with api_env(tmp_path, simulator=False, sensors=True) as env:
        socket = await connect(env)
        await env.vt.advance(1200)
        message = socket.of_type("health")[-1]
        assert list(message) == ["readings"]
        rest = (await env.client.get("/api/health/current")).json()
        strip = lambda data: [  # noqa: E731
            {k: v for k, v in r.items() if k != "read_at_us"} for r in data["readings"]
        ]
    assert strip(message) == strip(rest)  # same names, values, units and states
    for sent, current in zip(message["readings"], rest["readings"], strict=True):
        assert 0 < sent["read_at_us"] <= current["read_at_us"]  # never newer than the REST copy


async def test_the_snapshot_includes_the_health_readings(tmp_path):
    async with api_env(tmp_path, simulator=False, sensors=True) as env:
        await env.vt.advance(1500)
        socket = await connect(env)
        snapshot = socket.sent[0]["data"]
        assert snapshot["health"] == (await env.client.get("/api/health/current")).json()


async def test_first_data_arrives_within_three_seconds_of_connecting(tmp_path):
    async with api_env(tmp_path, simulator=False, sensors=True) as env:
        socket = await connect(env)
        await env.vt.advance(2999)
        values = [
            r for m in socket.of_type("health") for r in m["readings"] if r["value"] is not None
        ]
        first_snapshot = [r for r in socket.sent[0]["data"]["health"]["readings"] if r["value"]]
        assert values or first_snapshot


async def test_a_failed_sensor_shows_unavailable_in_the_next_health_message(tmp_path):
    async with api_env(tmp_path, simulator=False, sensors=True) as env:
        socket = await connect(env)
        await env.vt.advance(1500)
        await post(env, "/api/dev/sensors/ram_used/fail", {"on": True})
        await env.vt.advance(2500)
        latest = {r["name"]: r for r in socket.of_type("health")[-1]["readings"]}
        assert latest["ram_used"]["state"] == "unavailable" and latest["ram_used"]["value"] is None
        assert latest["cpu_load"]["state"] == "available"


# ---- wiring ----


async def test_gauges_are_saved_through_the_metrics_module(tmp_path):
    async with api_env(tmp_path, simulator=False, sensors=True) as env:
        await env.vt.advance(11_000)
        conn = env.query()
        summary = queries.metric_summary(conn)
        conn.close()
        gauges = {g.name: g for g in summary.gauges}
        assert set(gauges) == {f"laptop_{n}" for n in NAMES}
        assert gauges["laptop_cpu_temp"].unit == "celsius"


async def test_the_health_collector_stops_with_the_platform(tmp_path):
    async with api_env(tmp_path, simulator=False, sensors=True) as env:
        assert env.platform.health_running is True
    assert env.platform.health_running is False


@pytest.mark.parametrize("path", ["/api/health/current"])
async def test_health_responses_contain_no_secret_or_image_fields(tmp_path, path):
    async with api_env(tmp_path, simulator=False, sensors=True) as env:
        text = (await env.client.get(path)).text.lower()
        for word in ("password", "token", "secret", "image", "jpeg"):
            assert word not in text
