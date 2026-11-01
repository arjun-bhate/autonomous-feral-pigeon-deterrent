"""Tests for the web app basics (T054): status endpoints, error shape, pages, loopback only."""

import json
import re

import pytest

from deterrent.api import app as app_module
from support.api_env import api_env

FORBIDDEN_NAME = re.compile(r"password|secret|token|api_?key|authorization|image|jpeg|pixels", re.I)


def assert_clean(payload) -> None:
    """No response may carry secret-looking fields, image-looking fields or raw bytes."""

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                assert not FORBIDDEN_NAME.search(key), key
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        else:
            assert not isinstance(value, bytes | bytearray)

    walk(payload)
    assert "data:image" not in json.dumps(payload)


@pytest.fixture
def frontend(tmp_path):
    root = tmp_path / "frontend"
    (root / "css").mkdir(parents=True)
    (root / "css" / "app.css").write_text("body{}", encoding="utf-8")
    for name in ("index", "logs", "traces", "observability"):
        (root / f"{name}.html").write_text(
            f"<html><body>{name} page</body></html>", encoding="utf-8"
        )
    return root


# ---- device and system status ----


async def test_device_status_with_the_simulator_stopped_is_exactly_not_running(tmp_path):
    async with api_env(tmp_path, simulator=False) as env:
        response = await env.client.get("/api/device/status")
        assert response.status_code == 200
        assert response.json() == {"running": False}


async def test_running_device_status_has_every_documented_field(tmp_path):
    async with api_env(tmp_path) as env:
        data = (await env.client.get("/api/device/status")).json()
        assert set(data) == {
            "running",
            "state",
            "pan_cdeg",
            "tilt_cdeg",
            "key_on",
            "armed",
            "homed",
            "zone_store_valid",
            "effector_on",
            "fault_mask",
            "zone_count",
            "updated_us",
        }
        assert data["running"] is True and data["state"] == "DISARMED" and data["homed"] is True
        assert_clean(data)


async def test_device_status_after_the_simulator_stops_is_not_running_again(tmp_path):
    async with api_env(tmp_path) as env:
        await env.platform.stop_simulator()
        assert (await env.client.get("/api/device/status")).json() == {"running": False}


async def test_system_status_shows_storage_warning_and_dev_tools(tmp_path):
    async with api_env(tmp_path, simulator=False) as env:
        data = (await env.client.get("/api/system/status")).json()
        assert data == {"simulator_running": False, "storage_warning": None, "dev_tools": True}
        env.platform.set_storage_mode("locked")
        env.platform.logger.info("api", "this write will fail")
        env.platform.store.pump()
        data = (await env.client.get("/api/system/status")).json()
        assert set(data["storage_warning"]) == {"message", "since_us"}
        assert data["storage_warning"]["message"]
        assert_clean(data)


async def test_system_status_reports_dev_tools_off(tmp_path):
    async with api_env(tmp_path, simulator=False, dev_tools=False) as env:
        assert (await env.client.get("/api/system/status")).json()["dev_tools"] is False


async def test_status_endpoints_do_not_wait_for_a_failing_disk(tmp_path):
    async with api_env(tmp_path) as env:
        env.platform.set_storage_mode("locked")
        for _ in range(20):
            env.platform.logger.info("api", "noise")
        env.platform.store.pump()
        assert (await env.client.get("/api/device/status")).status_code == 200
        assert (await env.client.get("/api/system/status")).status_code == 200


# ---- one error shape ----


async def test_an_unknown_route_returns_the_standard_error_shape(tmp_path):
    async with api_env(tmp_path, simulator=False) as env:
        response = await env.client.get("/api/nothing/here")
        assert response.status_code == 404
        body = response.json()
        assert set(body) == {"error"} and set(body["error"]) == {"code", "message"}
        assert body["error"]["code"] == "NOT_FOUND" and body["error"]["message"]


async def test_a_wrong_method_also_uses_the_error_shape(tmp_path):
    async with api_env(tmp_path, simulator=False) as env:
        response = await env.client.post("/api/device/status")
        assert response.status_code == 405
        assert set(response.json()["error"]) == {"code", "message"}


# ---- loopback only ----


def test_the_server_settings_bind_loopback_only():
    assert app_module.HOST == "127.0.0.1"
    assert app_module.PORT == 8000


def test_running_the_server_always_passes_the_loopback_host(monkeypatch):
    calls = {}
    monkeypatch.setattr(app_module.uvicorn, "run", lambda *a, **kw: calls.update(kw, args=a))
    app_module.main()
    assert calls["host"] == "127.0.0.1" and calls["port"] == 8000


async def test_a_request_for_another_host_name_is_refused(tmp_path):
    async with api_env(tmp_path, simulator=False) as env:
        response = await env.client.get("/api/system/status", headers={"host": "evil.example"})
        assert response.status_code == 400
        response = await env.client.get("/api/system/status", headers={"host": "localhost:8000"})
        assert response.status_code == 200


# ---- pages and static files ----


@pytest.mark.parametrize(
    ("url", "text"),
    [
        ("/", "index page"),
        ("/logs", "logs page"),
        ("/traces", "traces page"),
        ("/observability", "observability page"),
    ],
)
async def test_page_routes_serve_their_html_file(tmp_path, frontend, url, text):
    async with api_env(tmp_path, simulator=False, frontend_dir=frontend) as env:
        response = await env.client.get(url)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert text in response.text


async def test_static_files_are_served_from_the_frontend_folder(tmp_path, frontend):
    async with api_env(tmp_path, simulator=False, frontend_dir=frontend) as env:
        response = await env.client.get("/static/css/app.css")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/css")


async def test_a_page_that_does_not_exist_yet_is_a_standard_404(tmp_path):
    empty = tmp_path / "empty_frontend"
    empty.mkdir()
    async with api_env(tmp_path, simulator=False, frontend_dir=empty) as env:
        response = await env.client.get("/logs")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "PAGE_NOT_FOUND"


async def test_static_files_cannot_escape_the_frontend_folder(tmp_path, frontend):
    (tmp_path / "secret.txt").write_text("top secret", encoding="utf-8")
    async with api_env(tmp_path, simulator=False, frontend_dir=frontend) as env:
        response = await env.client.get("/static/../secret.txt")
        assert "top secret" not in response.text


# ---- lifecycle ----


async def test_the_app_starts_and_stops_the_platform_with_its_lifespan(tmp_path):
    from deterrent.api.app import create_app
    from support.platform_env import Env

    env = Env(tmp_path)
    app = create_app(env.platform.settings, platform=env.platform)
    assert env.platform.started is False
    async with app.router.lifespan_context(app):
        assert env.platform.started is True
    assert env.platform.store_closed is True
