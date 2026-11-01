"""Tests for the static frontend files (T060, T061).

JavaScript is NOT run by pytest in this feature. These tests check that the server serves
the files with the right content types and that the source text contains the documented
constants and handlers. How the page behaves in a browser is checked by hand (see the
owner checklist in the T060/T061 report and quickstart.md).
"""

import hashlib
import re
from pathlib import Path

import pytest

from support.api_env import api_env

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def read(relative: str) -> str:
    return (FRONTEND / relative).read_text(encoding="utf-8")


# ---- served with the right content types ----


@pytest.mark.parametrize(
    ("url", "content_type"),
    [
        ("/", "text/html"),
        ("/static/css/app.css", "text/css"),
        ("/static/js/live.js", "javascript"),
        ("/static/vendor/chart.umd.min.js", "javascript"),
    ],
)
async def test_the_server_serves_the_page_css_js_and_vendor_file(tmp_path, url, content_type):
    async with api_env(tmp_path, simulator=False) as env:
        response = await env.client.get(url)
        assert response.status_code == 200
        assert content_type in response.headers["content-type"]
        assert len(response.content) > 100


async def test_the_base_page_links_the_stylesheet_and_has_the_navigation(tmp_path):
    async with api_env(tmp_path, simulator=False) as env:
        html = (await env.client.get("/")).text
        assert "/static/css/app.css" in html
        for link in ('href="/"', 'href="/logs"', 'href="/traces"', 'href="/observability"'):
            assert link in html


# ---- live.js: documented constants and behaviour (text tests) ----


def test_live_js_exports_the_reconnect_schedule_as_a_documented_constant():
    source = read("js/live.js")
    match = re.search(
        r"((?://[^\n]*\n)+)export const RECONNECT_DELAYS_MS = \[1000, 2000, 4000\];", source
    )
    assert match, "RECONNECT_DELAYS_MS must be [1000, 2000, 4000] with a comment above it"
    assert "1 s" in match.group(1) and "2 s" in match.group(1) and "4 s" in match.group(1)


def test_live_js_treats_three_seconds_without_a_message_as_disconnected():
    source = read("js/live.js")
    assert re.search(r"((?://[^\n]*\n)+)export const DISCONNECT_AFTER_MS = 3000;", source), (
        "DISCONNECT_AFTER_MS must be 3000 with a comment above it"
    )


def test_live_js_repeats_the_last_delay_after_the_list_ends():
    source = read("js/live.js")
    assert "export function nextReconnectDelay(attempt)" in source
    assert "Math.min(attempt, RECONNECT_DELAYS_MS.length - 1)" in source


def test_live_js_uses_one_websocket_to_the_live_endpoint():
    source = read("js/live.js")
    assert source.count("new WebSocket(") == 1
    assert "/ws/live" in source
    assert "location.host" in source  # never a hard-coded address


def test_live_js_restarts_the_three_second_timer_on_every_message_and_closes_on_expiry():
    source = read("js/live.js")
    assert "DISCONNECT_AFTER_MS" in source.split("export function connectLive")[1]
    assert "clearTimeout" in source and "socket.close()" in source


def test_live_js_replaces_everything_with_the_snapshot_after_each_connect():
    source = read("js/live.js")
    assert '"snapshot"' in source
    assert "emit(" in source
    # the attempt counter is reset only once a snapshot has really arrived
    assert re.search(r'snapshot"[^}]*attempt = 0', source, re.S)


def test_live_js_marks_the_connection_indicator_with_a_state():
    source = read("js/live.js")
    assert "connection-indicator" in source
    for state in ("connecting", "connected", "disconnected"):
        assert f'"{state}"' in source
    assert "dataset.state" in source


def test_live_js_never_builds_html_from_server_text():
    source = read("js/live.js")
    for risky in ("innerHTML", "eval(", "document.write", "insertAdjacentHTML"):
        assert risky not in source


# ---- css ----


def test_the_stylesheet_styles_the_three_connection_states_and_red_for_disconnected():
    css = read("css/app.css")
    for state in ("connecting", "connected", "disconnected"):
        assert f'[data-state="{state}"]' in css
    disconnected = css.split('[data-state="disconnected"]')[1].split("}")[0]
    assert "--bad" in disconnected or "red" in disconnected
    assert ".unavailable" in css


# ---- the vendored Chart.js file ----


def test_chartjs_is_vendored_with_its_version_and_source_recorded():
    library = FRONTEND / "vendor" / "chart.umd.min.js"
    readme = read("vendor/README.md")
    head = library.read_bytes()[:200].decode("utf-8", "replace")
    version = re.search(r"Chart\.js v(4\.\d+\.\d+)", head)
    assert version, "the file must be Chart.js 4.x"
    assert f"chart.js@{version.group(1)}" in readme
    assert "https://cdn.jsdelivr.net/npm/chart.js@" in readme
    assert "chart.umd.min.js" in readme


def test_the_recorded_checksum_matches_the_vendored_file():
    library = FRONTEND / "vendor" / "chart.umd.min.js"
    readme = read("vendor/README.md")
    recorded = re.search(r"sha256:\s*([0-9a-f]{64})", readme)
    assert recorded, "the README must record the file's sha256"
    assert hashlib.sha256(library.read_bytes()).hexdigest() == recorded.group(1)
