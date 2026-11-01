"""Tests for the developer routes (T056)."""

import ast
import asyncio
from pathlib import Path

import pytest

from deterrent.api import routes_dev
from deterrent.observability import queries
from deterrent.protocol.crc import zone_crc32
from support.api_env import api_env

ALL_ROUTES = [
    ("/api/dev/simulator/start", None),
    ("/api/dev/simulator/stop", None),
    ("/api/dev/key", {"on": True}),
    ("/api/dev/person", {"clear": False}),
    ("/api/dev/command", {"type": "STATUS_REQUEST", "fields": {}}),
    ("/api/dev/engagement", {"fire": False, "duration_ms": 500}),
    ("/api/dev/zone-store/corrupt", None),
    ("/api/dev/storage/fail", {"mode": "ok"}),
]


async def post(env, path: str, body: dict | None = None, ms: int = 60):
    return await env.send(lambda: env.client.post(path, json=body), ms)


async def command(env, kind: str, fields: dict | None = None, ms: int = 60):
    return await post(env, "/api/dev/command", {"type": kind, "fields": fields or {}}, ms)


# ---- off by default switch ----


@pytest.mark.parametrize(("path", "body"), ALL_ROUTES)
async def test_every_route_is_404_when_dev_tools_are_off(tmp_path, path, body):
    async with api_env(tmp_path, simulator=False, dev_tools=False) as env:
        response = await post(env, path, body)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_a_dev_route_is_indistinguishable_from_an_unknown_route_when_off(tmp_path):
    async with api_env(tmp_path, simulator=False, dev_tools=False) as env:
        off = (await post(env, "/api/dev/key", {"on": True})).json()
        unknown = (await post(env, "/api/dev/nothing", {})).json()
        assert off == unknown


# ---- simulator start and stop ----


async def test_simulator_start_leads_through_boot_and_homing_to_disarmed(tmp_path):
    async with api_env(tmp_path, simulator=False) as env:
        assert (await env.client.get("/api/device/status")).json() == {"running": False}
        response = await post(env, "/api/dev/simulator/start")
        assert response.status_code == 200 and response.json() == {"started": True}
        await env.vt.advance(3200)
        status = (await env.client.get("/api/device/status")).json()
        assert status["running"] is True and status["state"] == "DISARMED"
        conn = env.query()
        changes = [
            i.message
            for i in queries.query_logs(conn, component="simulator", limit=100).items
            if i.message.startswith("state changed")
        ]
        conn.close()
        assert changes == [
            "state changed BOOT -> HOMING",
            "state changed HOMING -> DISARMED",
        ]


async def test_starting_twice_is_a_conflict(tmp_path):
    async with api_env(tmp_path) as env:
        response = await post(env, "/api/dev/simulator/start")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "SIMULATOR_RUNNING"


async def test_stop_makes_the_device_status_not_running(tmp_path):
    async with api_env(tmp_path) as env:
        response = await post(env, "/api/dev/simulator/stop")
        assert response.status_code == 200 and response.json() == {"stopped": True}
        assert (await env.client.get("/api/device/status")).json() == {"running": False}


async def test_stopping_when_stopped_is_harmless(tmp_path):
    async with api_env(tmp_path, simulator=False) as env:
        assert (await post(env, "/api/dev/simulator/stop")).status_code == 200


# ---- inputs: key and person ----


async def test_the_key_route_sets_the_simulated_key_input(tmp_path):
    async with api_env(tmp_path) as env:
        assert (await post(env, "/api/dev/key", {"on": True})).json() == {"on": True}
        await env.vt.advance(50)
        assert (await env.client.get("/api/device/status")).json()["key_on"] is True


async def test_the_person_route_sets_what_the_heartbeat_carries(tmp_path):
    async with api_env(tmp_path) as env:
        assert (await post(env, "/api/dev/person", {"clear": False})).json() == {"clear": False}
        await env.vt.advance(400)
        assert env.platform.device.person_snapshot().person_clear is False
        await post(env, "/api/dev/person", {"clear": True})
        await env.vt.advance(400)
        assert env.platform.device.person_snapshot().person_clear is True


async def test_key_and_command_need_a_running_simulator(tmp_path):
    async with api_env(tmp_path, simulator=False) as env:
        for path, body in [
            ("/api/dev/key", {"on": True}),
            ("/api/dev/command", {"type": "STATUS_REQUEST", "fields": {}}),
        ]:
            response = await post(env, path, body)
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "SIMULATOR_NOT_RUNNING"


# ---- command ----


async def test_a_duration_of_499_returns_the_devices_field_range_reject(tmp_path):
    async with api_env(tmp_path) as env:
        response = await command(
            env,
            "FIRE_REQUEST",
            {"ttl_ms": 500, "intended_pan_cdeg": 0, "intended_tilt_cdeg": 0, "duration_ms": 499},
        )
        assert response.status_code == 200
        assert response.json() == {
            "type": "FIRE_REQUEST",
            "result": "REJECTED",
            "reject": "FIELD_RANGE",
        }


async def test_a_fire_request_reply_is_exactly_what_the_device_produced(tmp_path):
    async with api_env(tmp_path) as env:
        fields = {
            "ttl_ms": 500,
            "intended_pan_cdeg": 0,
            "intended_tilt_cdeg": 0,
            "duration_ms": 500,
        }
        body = (await command(env, "FIRE_REQUEST", fields)).json()
        assert body == {
            "type": "FIRE_REQUEST",
            "result": "DENIED",
            "denial_reason": "KEY_OFF",
            "faults": [],
        }
        assert env.counter("fire_requests_denied", reason="KEY_OFF") == 1  # the device counted it


async def test_a_full_fire_sequence_through_the_command_route(tmp_path):
    async with api_env(tmp_path) as env:
        await post(env, "/api/dev/key", {"on": True})
        arm = (await command(env, "ARM")).json()
        assert arm == {"type": "ARM", "result": "OK"}
        await env.vt.advance(6000)
        fire = {"ttl_ms": 500, "intended_pan_cdeg": 0, "intended_tilt_cdeg": 0, "duration_ms": 500}
        granted = (await command(env, "FIRE_REQUEST", fire)).json()
        assert granted["result"] == "GRANTED" and "granted_on_ms" in granted
        assert (await command(env, "FIRE_STOP")).json() == {"type": "FIRE_STOP", "result": "OK"}
        denied = (await command(env, "FIRE_REQUEST", fire)).json()
        assert denied["denial_reason"] == "COOLDOWN_ACTIVE"
        assert (await command(env, "DISARM")).json()["result"] == "OK"


async def test_move_returns_the_decoded_angles(tmp_path):
    async with api_env(tmp_path) as env:
        body = (await command(env, "MOVE", {"pan_cdeg": 6000, "tilt_cdeg": 2000})).json()
        assert body == {
            "type": "MOVE",
            "result": "OK",
            "data": {"pan_cdeg": 6000, "tilt_cdeg": 2000},
        }
        jog = (await command(env, "JOG", {"dpan_cdeg": 100, "dtilt_cdeg": -50})).json()
        assert jog["data"] == {"pan_cdeg": 6100, "tilt_cdeg": 1950}
        refused = (await command(env, "MOVE", {"pan_cdeg": 24001, "tilt_cdeg": 0})).json()
        assert refused["reject"] == "OUT_OF_SOFT_LIMIT"


async def test_heartbeat_status_and_zone_list_replies_are_decoded(tmp_path):
    async with api_env(tmp_path) as env:
        heartbeat = (await command(env, "HEARTBEAT", {"person_clear": 1})).json()
        assert heartbeat["data"]["state"] == "DISARMED" and "device_ms" in heartbeat["data"]
        status = (await command(env, "STATUS_REQUEST")).json()["data"]
        assert status["state"] == "DISARMED" and status["homed"] is True
        assert (await command(env, "ZONE_LIST")).json()["data"] == {"zones": []}


async def test_a_boolean_field_of_2_is_passed_through_and_rejected_by_the_device(tmp_path):
    async with api_env(tmp_path) as env:
        body = (await command(env, "HEARTBEAT", {"person_clear": 2})).json()
        assert body["result"] == "REJECTED" and body["reject"] == "FIELD_RANGE"


async def test_a_zone_can_be_uploaded_and_listed_through_commands(tmp_path):
    points = [(1000, 1000), (3000, 1000), (3000, 3000), (1000, 3000)]
    async with api_env(tmp_path) as env:
        assert (await command(env, "ZONE_BEGIN", {"zone_id": 1, "point_count": 4})).json()[
            "result"
        ] == "OK"
        for index, (pan, tilt) in enumerate(points):
            await command(env, "ZONE_POINT", {"index": index, "pan_cdeg": pan, "tilt_cdeg": tilt})
        commit = await command(env, "ZONE_COMMIT", {"crc32": zone_crc32(1, points)})
        assert commit.json()["result"] == "OK"
        zones = (await command(env, "ZONE_LIST")).json()["data"]["zones"]
        assert [(z["zone_id"], z["point_count"]) for z in zones] == [(1, 4)]
        assert (await command(env, "ZONE_DELETE", {"zone_id": 1})).json()["result"] == "OK"
        assert (await command(env, "ZONE_CLEAR")).json()["result"] == "OK"


async def test_an_unknown_command_type_or_bad_fields_are_400(tmp_path):
    async with api_env(tmp_path) as env:
        cases = [
            ({"type": "TELEPORT", "fields": {}}, "UNKNOWN_COMMAND"),
            ({"type": "MOVE", "fields": {"pan_cdeg": 1}}, "MISSING_FIELD"),
            ({"type": "MOVE", "fields": {"pan_cdeg": 1, "tilt_cdeg": 1, "x": 1}}, "UNKNOWN_FIELD"),
            ({"type": "MOVE", "fields": {"pan_cdeg": "a", "tilt_cdeg": 1}}, "INVALID_FIELD"),
            (
                {"type": "MOVE", "fields": {"pan_cdeg": 99999, "tilt_cdeg": 1}},
                "FIELD_OUT_OF_WIRE_RANGE",
            ),
        ]
        for body, code in cases:
            response = await post(env, "/api/dev/command", body)
            assert response.status_code == 400, body
            assert response.json()["error"]["code"] == code


async def test_a_missing_reply_is_a_gateway_timeout(tmp_path):
    async with api_env(tmp_path, faults=True) as env:
        env.faults.to_host.drop_next()
        response = await command(env, "STATUS_REQUEST", ms=600)
        assert response.status_code == 504
        assert response.json()["error"]["code"] == "DEVICE_TIMEOUT"


# ---- engagement ----


async def test_engagement_returns_a_trace_id_that_exists(tmp_path):
    async with api_env(tmp_path) as env:
        response = await post(env, "/api/dev/engagement", {"fire": True, "duration_ms": 500})
        assert response.status_code == 200
        trace_id = response.json()["trace_id"]
        conn = env.query()
        trace = queries.get_trace(conn, trace_id)
        conn.close()
        assert trace is not None and trace.outcome == "DENIED" and trace.deny_reason == "KEY_OFF"


async def test_engagement_passes_the_duration_to_the_device_unchanged(tmp_path):
    async with api_env(tmp_path) as env:
        trace_id = (
            await post(env, "/api/dev/engagement", {"fire": True, "duration_ms": 499})
        ).json()["trace_id"]
        conn = env.query()
        trace = queries.get_trace(conn, trace_id)
        conn.close()
        assert trace.outcome == "REJECTED"


async def test_engagement_without_fire_needs_no_simulator_but_with_fire_it_does(tmp_path):
    async with api_env(tmp_path, simulator=False) as env:
        ok = await post(env, "/api/dev/engagement", {"fire": False})
        assert ok.status_code == 200
        response = await post(env, "/api/dev/engagement", {"fire": True})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "SIMULATOR_NOT_RUNNING"


# ---- the test inputs ----


async def test_corrupting_the_zone_store_needs_a_stored_zone(tmp_path):
    points = [(1000, 1000), (3000, 1000), (3000, 3000)]
    async with api_env(tmp_path) as env:
        none = await post(env, "/api/dev/zone-store/corrupt")
        assert none.status_code == 409 and none.json()["error"]["code"] == "NO_ZONE_STORED"
        await command(env, "ZONE_BEGIN", {"zone_id": 4, "point_count": 3})
        for index, (pan, tilt) in enumerate(points):
            await command(env, "ZONE_POINT", {"index": index, "pan_cdeg": pan, "tilt_cdeg": tilt})
        await command(env, "ZONE_COMMIT", {"crc32": zone_crc32(4, points)})
        assert (await post(env, "/api/dev/zone-store/corrupt")).json() == {"corrupted": True}
        await env.vt.advance(1100)
        assert (await env.client.get("/api/device/status")).json()["zone_store_valid"] is False


async def test_storage_fail_raises_and_clears_the_warning(tmp_path):
    async with api_env(tmp_path, simulator=False) as env:
        assert (await post(env, "/api/dev/storage/fail", {"mode": "locked"})).json() == {
            "mode": "locked"
        }
        env.platform.logger.info("api", "a write that fails")
        env.platform.store.pump()
        assert (await env.client.get("/api/system/status")).json()["storage_warning"] is not None
        await post(env, "/api/dev/storage/fail", {"mode": "ok"})
        await env.vt.advance(2100)
        env.platform.store.pump()
        assert (await env.client.get("/api/system/status")).json()["storage_warning"] is None


async def test_a_bad_storage_mode_uses_the_standard_error_shape(tmp_path):
    async with api_env(tmp_path, simulator=False) as env:
        response = await post(env, "/api/dev/storage/fail", {"mode": "exploded"})
        assert response.status_code == 422
        assert set(response.json()["error"]) == {"code", "message"}
        assert response.json()["error"]["code"] == "INVALID_REQUEST"


# ---- nothing here can force a grant ----


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/dev/engagement", {"fire": True, "grant": True}),
        ("/api/dev/engagement", {"fire": True, "force": True, "duration_ms": 500}),
        ("/api/dev/key", {"on": True, "grant": True}),
        ("/api/dev/person", {"clear": True, "override": True}),
        ("/api/dev/command", {"type": "FIRE_REQUEST", "fields": {}, "granted": True}),
        ("/api/dev/command", {"type": "FIRE_REQUEST", "fields": {}, "force": True}),
    ],
)
async def test_extra_parameters_that_might_force_a_grant_are_refused(tmp_path, path, body):
    async with api_env(tmp_path) as env:
        response = await post(env, path, body)
        assert response.status_code == 422
        assert env.counter("fire_requests_granted") == 0


async def test_fire_request_fields_cannot_carry_a_grant_flag(tmp_path):
    async with api_env(tmp_path) as env:
        fields = {
            "ttl_ms": 500,
            "intended_pan_cdeg": 0,
            "intended_tilt_cdeg": 0,
            "duration_ms": 500,
            "granted": True,
        }
        response = await command(env, "FIRE_REQUEST", fields)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "UNKNOWN_FIELD"


def test_the_dev_routes_module_has_no_grant_path_of_its_own():
    source = Path(routes_dev.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not [name for name in imported if name.startswith("deterrent.simulator")]
    for forbidden in ("Reply.granted", "decide_fire", "_start_shot", "effector_on ="):
        assert forbidden not in source


async def test_concurrent_dev_requests_each_get_their_own_reply(tmp_path):
    async with api_env(tmp_path) as env:
        tasks = [
            asyncio.create_task(
                env.client.post("/api/dev/command", json={"type": "STATUS_REQUEST", "fields": {}})
            )
            for _ in range(3)
        ]
        await env.vt.advance(60)
        responses = [await t for t in tasks]
        assert [r.json()["result"] for r in responses] == ["OK"] * 3
