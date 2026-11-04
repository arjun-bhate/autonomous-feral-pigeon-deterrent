"""Privacy and local-only tests (T072).

The rules (constitution VIII, FR-012, FR-014):

* no log, trace, metric or API response holds image data or a secret-named field;
* runtime data (``data/`` and ``*.sqlite3``) never shows up in ``git status``;
* the server listens on the loopback address only;
* the application makes no network call at all (tested with sockets blocked).
"""

import ast
import asyncio
import json
import re
import socket
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from deterrent.api import app as app_module
from deterrent.api.app import HOST, create_app
from deterrent.composition import Platform
from deterrent.config import load_settings
from support.api_env import api_env
from support.fake_socket import FakeSocket

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "src" / "deterrent"

SECRET_KEY = re.compile(
    r"password|passwd|secret|token|api[_-]?key|authorization|credential|private[_-]?key|cookie",
    re.IGNORECASE,
)
IMAGE_KEY = re.compile(
    r"image|pixel|jpe?g|png|bitmap|footage|video|thumbnail|base64|raw_frame", re.IGNORECASE
)
IMAGE_VALUE_MARKERS = (
    "data:image/",
    "/9j/",
    "iVBORw0KGgo",
    "JFIF",
    "\xff\xd8\xff",
    "\x89PNG",
    "GIF8",
)
BASE64_RUN = re.compile(r"[A-Za-z0-9+/=]{200,}")


# ---- the scanner -------------------------------------------------------------------------------


def privacy_problems(value, path="$") -> list[str]:
    """Walk any JSON-like value and report image data or secret-named / image-named keys."""
    problems = []
    if isinstance(value, dict):
        for key, item in value.items():
            if SECRET_KEY.search(str(key)):
                problems.append(f"{path}.{key}: a secret-named field")
            if IMAGE_KEY.search(str(key)):
                problems.append(f"{path}.{key}: an image-named field")
            problems.extend(privacy_problems(item, f"{path}.{key}"))
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            problems.extend(privacy_problems(item, f"{path}[{index}]"))
    elif isinstance(value, bytes | bytearray):
        problems.append(f"{path}: raw bytes")
    elif isinstance(value, str):
        if any(marker in value for marker in IMAGE_VALUE_MARKERS):
            problems.append(f"{path}: looks like image data")
        if BASE64_RUN.search(value):
            problems.append(f"{path}: a long base64-like run")
    return problems


def test_the_scanner_itself_catches_secrets_and_images():
    assert privacy_problems({"a": {"password": "x"}})
    assert privacy_problems({"api_key": 1}) and privacy_problems({"Authorization": "x"})
    assert privacy_problems({"jpeg_data": "x"}) and privacy_problems({"image": "x"})
    assert privacy_problems(["data:image/png;base64,AAAA"])
    assert privacy_problems("/9j/4AAQSkZJRg==")
    assert privacy_problems(b"\xff\xd8\xff\xe0")
    assert privacy_problems("A" * 300)
    assert privacy_problems({"ok": [1, "plain text", {"trace_id": "0123456789abcdef"}]}) == []


# ---- every kind of record and response ---------------------------------------------------------


async def busy_session(env) -> dict[str, object]:
    """Use the system the way the owner does and collect everything it says."""
    socket_ = FakeSocket()
    serving = asyncio.create_task(env.app.state.hub.serve(socket_))
    await env.vt.advance(500)
    await env.arm_and_wait_ready()
    for body in ({"fire": True}, {"fire": False}, {"fire": True, "duration_ms": 499}):
        await env.send(lambda b=body: env.client.post("/api/dev/engagement", json=b), 150)
        await env.vt.advance(8000)
    await env.client.post("/api/dev/command", json={"type": "STATUS_REQUEST", "fields": {}})
    await env.vt.advance(12_000)  # health gauges are saved every 10 s
    env.platform.store.flush()

    seen: dict[str, object] = {}
    for path in (
        "/api/logs?limit=1000",
        "/api/logs/components",
        "/api/traces?limit=100",
        "/api/metrics",
        "/api/health/current",
        "/api/device/status",
        "/api/system/status",
    ):
        response = await env.client.get(path)
        assert response.status_code == 200, path
        seen[path] = response.json()
    for item in seen["/api/traces?limit=100"]["items"]:
        detail = await env.client.get(f"/api/traces/{item['trace_id']}")
        seen[f"trace {item['trace_id']}"] = detail.json()
    for gauge in seen["/api/metrics"]["gauges"]:
        series = await env.client.get(f"/api/metrics/{gauge['name']}/series")
        seen[f"series {gauge['name']}"] = series.json()
    seen["websocket"] = list(socket_.sent)
    socket_.disconnect()
    await serving
    return seen


async def test_no_api_response_or_live_message_holds_image_data_or_secret_names(tmp_path):
    async with api_env(tmp_path, sensors=True) as env:
        seen = await busy_session(env)
        assert len(seen["/api/logs?limit=1000"]["items"]) > 10  # the scan has something to scan
        assert seen["/api/traces?limit=100"]["items"] and seen["/api/metrics"]["counters"]
        assert any(m["type"] == "log" for m in seen["websocket"])
        for name, payload in seen.items():
            assert privacy_problems(payload, name) == [], name


async def test_the_database_files_hold_no_binary_or_image_columns_and_no_secret_names(tmp_path):
    async with api_env(tmp_path, sensors=True) as env:
        await busy_session(env)
        env.platform.store.flush()
        conn = sqlite3.connect(tmp_path / "observability.sqlite3")
        try:
            tables = [
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            ]
            assert {"logs", "traces", "spans", "metric_events"} <= set(tables)
            for table in tables:
                columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
                assert not [c for c in columns if SECRET_KEY.search(c) or IMAGE_KEY.search(c)]
                for column in columns:
                    blobs = conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE typeof({column}) = 'blob'"
                    ).fetchone()[0]
                    assert blobs == 0, f"{table}.{column} holds binary data"
                for row in conn.execute(f"SELECT * FROM {table}"):
                    assert privacy_problems(list(row), table) == []
        finally:
            conn.close()


async def test_a_secret_or_image_written_to_a_log_is_removed_before_it_is_stored(tmp_path):
    async with api_env(tmp_path, simulator=False) as env:
        env.platform.logger.info(
            "api",
            "attempt password=hunter2 now",
            password="hunter2",
            api_key="abcd1234",
            jpeg_data=b"\xff\xd8\xff\xe0data",
            note="ordinary",
        )
        env.platform.logger.info("api", "data:image/png;base64," + "A" * 400)
        env.platform.store.flush()
        body = json.dumps((await env.client.get("/api/logs?limit=1000")).json())
        assert "hunter2" not in body and "abcd1234" not in body
        assert "data:image" not in body and "\\u00ff\\u00d8" not in body
        assert "ordinary" in body  # normal fields are untouched


# ---- git never sees runtime data ----------------------------------------------------------------


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=False, timeout=60
    )


def test_git_status_shows_no_data_folder_or_sqlite_files():
    status = git("status", "--short", "--untracked-files=all")
    assert status.returncode == 0, status.stderr
    for line in status.stdout.splitlines():
        path = line[3:].strip().strip('"')
        assert not path.startswith("data/"), line
        assert not path.endswith(".sqlite3"), line


@pytest.mark.parametrize(
    "path",
    [
        "data/observability.sqlite3",
        "data/device/zone_store.sqlite3",
        "data/raw/clip.mp4",
        "anything/else.sqlite3",
    ],
)
def test_git_ignores_runtime_data_whatever_the_folder(path):
    assert git("check-ignore", "-q", path).returncode == 0, f"{path} is not ignored"


def test_no_database_file_is_tracked_by_git():
    tracked = git("ls-files").stdout.splitlines()
    assert not [p for p in tracked if p.endswith(".sqlite3") or p.startswith("data/")]


# ---- loopback only -------------------------------------------------------------------------------


def test_the_server_address_constant_is_the_loopback_address():
    assert HOST == "127.0.0.1"


def test_main_starts_uvicorn_on_the_loopback_address_only(monkeypatch):
    captured = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: captured.update(kw))
    app_module.main()
    assert captured["host"] == "127.0.0.1"
    assert captured["factory"] is True


def test_the_source_never_names_an_all_interfaces_address():
    for path in SOURCE.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "0.0.0.0" not in text, path
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "host":
                assert not (
                    isinstance(node.value, ast.Constant) and node.value.value in ("", "::")
                ), f"{path} binds to all interfaces"


def test_a_real_server_started_with_the_apps_host_listens_only_on_loopback(tmp_path):
    platform = Platform(load_settings(), data_dir=tmp_path)
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(platform=platform), host=HOST, port=0, log_level="warning", lifespan="on"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "the server did not start"
        bound = [s.getsockname() for srv in server.servers for s in srv.sockets]
        assert bound and all(address[0] == "127.0.0.1" for address in bound)
    finally:
        server.should_exit = True
        thread.join(timeout=15)


async def test_a_request_addressed_to_another_host_name_is_refused(tmp_path):
    async with api_env(tmp_path, simulator=False) as env:
        response = await env.client.get("/api/system/status", headers={"host": "evil.example"})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_HOST"


# ---- no network call -----------------------------------------------------------------------------

NETWORK_MODULES = {
    "requests",
    "urllib.request",
    "urllib3",
    "http.client",
    "httpx",
    "aiohttp",
    "websockets",
    "smtplib",
    "ftplib",
    "telnetlib",
    "socket",
    "xmlrpc.client",
}


def test_no_source_file_imports_a_network_client_library():
    found = []
    for path in SOURCE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            found.extend(
                f"{path.name}: {n}"
                for n in names
                if any(n == m or n.startswith(m + ".") for m in NETWORK_MODULES)
            )
    assert found == []


async def test_the_application_attempts_no_network_connection_or_name_lookup(tmp_path, monkeypatch):
    attempts: list[str] = []

    def blocked(name):
        def refuse(*args, **kwargs):
            attempts.append(f"{name}{args[1:] if name.endswith('connect') else args}")
            raise OSError(f"network blocked by the test: {name}")

        return refuse

    monkeypatch.setattr(socket.socket, "connect", blocked("socket.connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", blocked("socket.connect_ex"))
    monkeypatch.setattr(socket.socket, "sendto", blocked("socket.sendto"))
    monkeypatch.setattr(socket, "create_connection", blocked("create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", blocked("getaddrinfo"))
    monkeypatch.setattr(socket, "gethostbyname", blocked("gethostbyname"))

    with pytest.raises(OSError):  # prove the blocker works: it records and refuses
        socket.create_connection(("example.invalid", 80))
    assert len(attempts) == 1
    attempts.clear()

    async with api_env(tmp_path, sensors=True) as running:
        seen = await busy_session(running)
        assert seen["/api/traces?limit=100"]["items"]
        assert any(m["type"] == "tick" for m in seen["websocket"])
    assert attempts == []
