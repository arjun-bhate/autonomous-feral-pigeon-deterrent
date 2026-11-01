"""Tests for the live WebSocket hub (T055). Time is virtual; sockets are fakes."""

import asyncio

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from deterrent.api.app import create_app
from deterrent.api.live import LiveHub
from deterrent.composition import Platform
from deterrent.config import load_settings
from deterrent.protocol.messages import Move
from support.platform_env import Env

LOOPBACK = {"host": "127.0.0.1:8000"}


class FakeSocket:
    """Stands in for a browser connection."""

    def __init__(self, *, stuck: bool = False) -> None:
        self.sent: list[dict] = []
        self.accepted = False
        self._stuck = stuck
        self._inbox: asyncio.Queue[str | None] = asyncio.Queue()

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, data: dict) -> None:
        if self._stuck:
            await asyncio.Event().wait()  # a browser that never reads
        self.sent.append(data)

    async def receive_text(self) -> str:
        item = await self._inbox.get()
        if item is None:
            raise WebSocketDisconnect()
        return item

    def says(self, text: str) -> None:
        self._inbox.put_nowait(text)

    def disconnect(self) -> None:
        self._inbox.put_nowait(None)

    def of_type(self, kind: str) -> list[dict]:
        return [m["data"] for m in self.sent if m["type"] == kind]


@pytest.fixture
async def env(tmp_path):
    e = Env(tmp_path)
    await e.start()
    e.hub = LiveHub(e.platform)
    await e.hub.start()
    yield e
    await e.hub.stop()
    await e.stop()


async def connect(env: Env, **kw) -> FakeSocket:
    socket = FakeSocket(**kw)
    env.tasks = getattr(env, "tasks", [])
    env.tasks.append(asyncio.create_task(env.hub.serve(socket)))
    await env.vt.advance(1)
    return socket


# ---- connecting ----


async def test_the_snapshot_arrives_immediately_on_connect(env):
    socket = await connect(env)
    assert socket.accepted
    assert socket.sent[0]["type"] == "snapshot"
    data = socket.sent[0]["data"]
    assert set(data) >= {"device", "system", "recent_logs"}
    assert data["device"]["running"] is True and data["device"]["state"] == "DISARMED"
    assert data["system"]["simulator_running"] is True
    assert data["system"]["storage_warning"] is None


async def test_the_snapshot_holds_at_most_50_logs_newest_last(env):
    for index in range(80):
        env.platform.logger.info("api", f"line {index}")
    socket = await connect(env)
    logs = socket.sent[0]["data"]["recent_logs"]
    assert len(logs) == 50
    assert [item["message"] for item in logs][-1] == "line 79"
    assert logs == sorted(logs, key=lambda item: (item["ts_us"], item["id"]))
    assert set(logs[0]) == {
        "id",
        "ts_us",
        "component",
        "severity",
        "message",
        "trace_id",
        "span_id",
        "fields",
    }


async def test_the_snapshot_says_not_running_when_the_simulator_is_stopped(env):
    await env.platform.stop_simulator()
    socket = await connect(env)
    assert socket.sent[0]["data"]["device"] == {"running": False}


async def test_a_reconnecting_browser_gets_a_fresh_snapshot_with_current_values(env):
    first = await connect(env)
    first.disconnect()
    await env.vt.advance(10)
    await env.platform.client.request(Move(1234, 321))
    await env.vt.advance(50)
    second = await connect(env)
    assert second.sent[0]["type"] == "snapshot"
    assert second.sent[0]["data"]["device"]["pan_cdeg"] == 1234


# ---- device updates ----


async def test_a_pan_change_reaches_the_client_within_one_second(env):
    socket = await connect(env)
    before = len(socket.sent)
    task = asyncio.create_task(env.platform.client.request(Move(6000, 2000)))
    await env.vt.advance(1000)
    await task
    updates = [m["data"] for m in socket.sent[before:] if m["type"] == "device"]
    assert any(u["pan_cdeg"] == 6000 and u["tilt_cdeg"] == 2000 for u in updates)


async def test_the_first_update_after_a_change_comes_before_the_next_periodic_one(env):
    socket = await connect(env)
    await env.vt.advance(500)  # now somewhere inside a one-second period
    before = len(socket.sent)
    task = asyncio.create_task(env.platform.client.request(Move(4000, 0)))
    await env.vt.advance(60)
    await task
    update = [m["data"] for m in socket.sent[before:] if m["type"] == "device"]
    assert update and update[0]["pan_cdeg"] == 4000


async def test_device_updates_come_at_least_once_a_second_while_running(env):
    socket = await connect(env)
    before = len(socket.of_type("device"))
    await env.vt.advance(5000)
    assert len(socket.of_type("device")) - before >= 5


async def test_stopping_the_simulator_sends_device_offline(env):
    socket = await connect(env)
    await env.platform.stop_simulator()
    await env.vt.advance(10)
    assert [m["type"] for m in socket.sent][-1] == "device_offline"
    assert socket.sent[-1]["data"] == {}
    before = len(socket.sent)
    await env.vt.advance(3000)
    assert [m["type"] for m in socket.sent[before:] if m["type"] == "device"] == []


# ---- logs, storage warning, tick ----


async def test_a_new_log_entry_is_pushed(env):
    socket = await connect(env)
    env.platform.logger.warning("api", "something happened", detail=5)
    await env.vt.advance(10)
    pushed = [d for d in socket.of_type("log") if d["message"] == "something happened"]
    assert len(pushed) == 1
    assert pushed[0]["severity"] == "WARNING" and pushed[0]["fields"] == {"detail": 5}


async def test_storage_warning_is_pushed_when_raised_and_cleared_later(env):
    socket = await connect(env)
    env.platform.set_storage_mode("locked")
    env.platform.logger.info("api", "will fail")
    env.platform.store.pump()
    await env.vt.advance(10)
    raised = socket.of_type("storage_warning")
    assert len(raised) == 1 and raised[0]["message"]
    env.platform.set_storage_mode("ok")
    await env.vt.advance(2100)
    env.platform.store.pump()
    await env.vt.advance(10)
    warnings = socket.of_type("storage_warning")
    assert len(warnings) == 2 and warnings[1] is None


async def test_tick_arrives_every_second(env):
    socket = await connect(env)
    await env.vt.advance(3500)
    ticks = socket.of_type("tick")
    assert len(ticks) == 3
    assert [b["server_us"] - a["server_us"] for a, b in zip(ticks, ticks[1:], strict=False)] == [
        1_000_000,
        1_000_000,
    ]


# ---- several clients, slow clients, a locked disk ----


async def test_two_clients_receive_the_same_values(env):
    first, second = await connect(env), await connect(env)
    await env.platform.client.request(Move(2500, 100))
    await env.vt.advance(2500)
    a = [m for m in first.sent if m["type"] in ("device", "log", "tick")]
    b = [m for m in second.sent if m["type"] in ("device", "log", "tick")]
    assert a and a == b


async def test_a_client_that_never_reads_does_not_hold_up_the_others(env):
    stuck = await connect(env, stuck=True)
    healthy = await connect(env)
    await env.vt.advance(3200)
    assert len(healthy.of_type("tick")) == 3
    assert stuck.sent == []  # it never got anything through, and nothing waited for it


async def test_a_client_that_falls_far_behind_is_dropped(env):
    stuck = await connect(env, stuck=True)
    assert env.hub.client_count == 1
    for index in range(500):
        env.platform.logger.info("api", f"flood {index}")
    await env.vt.advance(50)
    assert env.hub.client_count == 0
    assert stuck.sent == []


async def test_with_the_store_locked_the_hub_still_sends_on_schedule(env):
    socket = await connect(env)
    env.platform.set_storage_mode("locked")
    for _ in range(10):
        env.platform.logger.info("api", "noise while locked")
    env.platform.store.pump()
    before = len(socket.of_type("tick"))
    task = asyncio.create_task(env.platform.client.request(Move(900, 0)))
    await env.vt.advance(1100)
    await task
    assert len(socket.of_type("tick")) - before == 1
    assert any(u["pan_cdeg"] == 900 for u in socket.of_type("device"))


# ---- server to client only ----


async def test_anything_the_client_sends_is_ignored(env):
    socket = await connect(env)
    before = len(socket.sent)
    socket.says('{"type": "fire"}')
    socket.says("hello")
    await env.vt.advance(10)
    assert [m["type"] for m in socket.sent[before:]] == []
    assert env.hub.client_count == 1


async def test_a_disconnected_client_is_forgotten(env):
    socket = await connect(env)
    assert env.hub.client_count == 1
    socket.disconnect()
    await env.vt.advance(10)
    assert env.hub.client_count == 0


async def test_stopping_the_hub_closes_nothing_it_does_not_own_and_is_repeatable(env):
    await connect(env)
    await env.hub.stop()
    await env.hub.stop()


# ---- the real WebSocket route ----


def make_real_app(tmp_path):
    platform = Platform(load_settings(), data_dir=tmp_path, start_store_thread=True)
    return create_app(platform.settings, platform=platform)


def test_the_websocket_route_sends_a_snapshot_first(tmp_path):
    app = make_real_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        with client.websocket_connect("/ws/live", headers=LOOPBACK) as socket:
            message = socket.receive_json()
            assert message["type"] == "snapshot"
            assert message["data"]["device"] == {"running": False}


def test_a_browser_reconnecting_after_a_server_restart_gets_a_fresh_snapshot(tmp_path):
    for _ in range(2):  # two separate server lifetimes
        app = make_real_app(tmp_path)
        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            with client.websocket_connect("/ws/live", headers=LOOPBACK) as socket:
                assert socket.receive_json()["type"] == "snapshot"


def test_the_websocket_route_refuses_another_host_name(tmp_path):
    app = make_real_app(tmp_path)
    with TestClient(app, base_url="http://evil.example") as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/live", headers={"host": "evil.example"}):
                pass
