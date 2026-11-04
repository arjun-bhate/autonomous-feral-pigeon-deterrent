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


# ---- T061: the health tiles on the Overview ----

SLOTS = ("cpu_load", "gpu_load", "ram_used", "cpu_temp", "gpu_temp")


def test_the_overview_has_an_element_for_each_of_the_five_health_slots():
    html = read("index.html")
    for slot in SLOTS:
        assert f'id="health-{slot}"' in html
        assert f'id="health-{slot}-value"' in html
        assert f'id="health-{slot}-unit"' in html


def test_the_slots_are_labelled_in_plain_words():
    html = read("index.html")
    for label in ("CPU load", "GPU load", "RAM use", "CPU temperature", "GPU temperature"):
        assert label in html


def test_the_overview_has_the_connection_indicator_and_loads_its_script_as_a_module():
    html = read("index.html")
    assert 'id="connection-indicator"' in html and 'data-state="' in html
    assert '<script type="module" src="/static/js/overview.js"></script>' in html


def test_overview_js_shows_the_word_unavailable_unless_the_reading_is_available():
    source = read("js/overview.js")
    assert 'const UNAVAILABLE_TEXT = "unavailable";' in source
    assert 'reading.state === "available"' in source and "reading.value !== null" in source
    assert 'classList.add("unavailable")' in source
    assert 'classList.remove("unavailable")' in source


def test_overview_js_shows_units_and_one_decimal():
    source = read("js/overview.js")
    assert 'percent: "%"' in source and 'celsius: "°C"' in source
    assert "toFixed(1)" in source


def test_overview_js_updates_from_health_messages_and_the_snapshot_without_reloading():
    source = read("js/overview.js")
    assert 'from "./live.js"' in source
    assert 'live.on("health"' in source
    assert 'live.on("snapshot"' in source
    assert "location.reload" not in source


def test_overview_js_replaces_all_health_slots_on_every_update():
    source = read("js/overview.js")
    assert "for (const slot of SLOTS)" in source  # every slot is drawn, even one not in the data
    for slot in SLOTS:
        assert f'"{slot}"' in source


def test_overview_js_shows_unavailable_everywhere_while_disconnected():
    source = read("js/overview.js")
    handler = source.split('live.on("connection"')[1]
    assert "disconnected" in handler and "showAllUnavailable" in handler


def test_overview_js_writes_text_only_never_html():
    source = read("js/overview.js")
    for risky in ("innerHTML", "eval(", "document.write", "insertAdjacentHTML"):
        assert risky not in source
    assert "textContent" in source


async def test_overview_js_is_served_as_javascript(tmp_path):
    async with api_env(tmp_path, simulator=False) as env:
        response = await env.client.get("/static/js/overview.js")
        assert response.status_code == 200
        assert "javascript" in response.headers["content-type"]


# ---- T063: the labelled placeholders and the recent-log list on the Overview ----


def test_the_overview_has_the_camera_placeholder_saying_what_will_appear_and_when():
    html = read("index.html")
    assert 'id="camera-placeholder"' in html
    assert "detection overlay" in html.lower()
    assert "later feature" in html.lower()


def test_the_overview_has_the_pi_health_panel_reading_not_connected():
    html = read("index.html")
    assert 'id="pi-health"' in html
    panel = html.split('id="pi-health"')[1].split("</section>")[0]
    assert "not connected" in panel.lower()


def test_the_overview_has_the_recent_log_list_element():
    html = read("index.html")
    assert 'id="recent-logs"' in html
    assert 'id="recent-logs-empty"' in html


def test_overview_js_fills_the_recent_logs_from_the_snapshot_and_log_messages():
    source = read("js/overview.js")
    assert "data.recent_logs" in source
    assert 'live.on("log"' in source
    assert "RECENT_LOG_LIMIT = 50" in source


def test_overview_js_shows_newest_first_and_keeps_only_fifty():
    source = read("js/overview.js")
    # the snapshot is newest LAST, so it is reversed; a new entry goes to the top
    assert ".reverse()" in source
    assert "RECENT_LOG_LIMIT" in source.split("function addLog")[1]


def test_overview_js_writes_log_text_with_textcontent_only():
    source = read("js/overview.js")
    assert "createElement" in source
    for risky in ("innerHTML", "insertAdjacentHTML", "document.write"):
        assert risky not in source


async def test_the_snapshot_holds_at_most_fifty_logs_newest_last_and_a_new_log_is_a_log_message(
    tmp_path,
):
    import asyncio

    from support.fake_socket import FakeSocket

    async with api_env(tmp_path) as env:
        for index in range(70):
            env.platform.logger.info("api", f"line {index}")
        socket = FakeSocket()
        task = asyncio.create_task(env.app.state.hub.serve(socket))
        await env.vt.advance(1)
        logs = socket.sent[0]["data"]["recent_logs"]
        assert len(logs) <= 50
        assert logs[-1]["message"] == "line 69"
        env.platform.logger.info("api", "a brand new line")
        await env.vt.advance(5)
        assert any(m["message"] == "a brand new line" for m in socket.of_type("log"))
        socket.disconnect()
        await task
