"""Tests for the live device panel on the Overview (T062).

The server side is tested with a fake WebSocket and virtual time. The JavaScript is not
run by pytest, so the page's behaviour is checked as source text, plus the owner's
browser checklist.
"""

import asyncio
import re
from pathlib import Path

from deterrent.protocol.codes import STATE_DISPLAY_NAMES, State
from support.api_env import api_env
from support.fake_socket import FakeSocket

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def read(relative: str) -> str:
    return (FRONTEND / relative).read_text(encoding="utf-8")


async def test_a_move_through_the_dev_route_shows_up_in_device_messages_within_one_second(
    tmp_path,
):
    async with api_env(tmp_path) as env:
        socket = FakeSocket()
        task = asyncio.create_task(env.app.state.hub.serve(socket))
        await env.vt.advance(1)
        before = len(socket.of_type("device"))
        response = await env.send(
            lambda: env.client.post(
                "/api/dev/command",
                json={"type": "MOVE", "fields": {"pan_cdeg": 1500, "tilt_cdeg": 300}},
            ),
            60,
        )
        assert response.status_code == 200
        await env.vt.advance(1000)
        seen = socket.of_type("device")[before:]
        assert any(m["pan_cdeg"] == 1500 and m["tilt_cdeg"] == 300 for m in seen)
        socket.disconnect()
        await task


def test_every_one_of_the_twelve_states_has_a_display_name_in_overview_js():
    source = read("js/overview.js")
    block = re.search(r"const STATE_NAMES = \{(.*?)\};", source, re.S)
    assert block, "overview.js must define STATE_NAMES"
    names = dict(re.findall(r"(\w+):\s*\"([^\"]+)\"", block.group(1)))
    assert set(names) == {state.name for state in State}
    assert len(names) == 12
    for state, shown in STATE_DISPLAY_NAMES.items():
        assert names[state.name] == shown
    assert names["SAFE_ERROR"] == "SAFE/ERROR"


def test_an_unknown_state_is_never_shown_as_a_raw_value():
    source = read("js/overview.js")
    # a state not in the map is displayed as "unavailable", never as the raw text or number
    assert "STATE_NAMES[" in source
    assert "Object.hasOwn(STATE_NAMES" in source


def test_index_html_has_ids_for_pan_tilt_state_and_the_not_running_label():
    html = read("index.html")
    for element in ("device-pan", "device-tilt", "device-state", "device-not-running"):
        assert f'id="{element}"' in html
    assert "simulator not running" in html.lower()


def test_overview_js_shows_degrees_from_centidegrees():
    source = read("js/overview.js")
    assert "/ 100" in source
    assert "toFixed(2)" in source or "toFixed(1)" in source


def test_overview_js_updates_the_panel_from_snapshot_and_device_messages():
    source = read("js/overview.js")
    assert 'live.on("device"' in source
    assert "data.device" in source


def test_device_offline_clears_pan_tilt_and_state_and_shows_the_label():
    source = read("js/overview.js")
    handler = source.split('live.on("device_offline"')[1].split("});")[0]
    assert "showDeviceOffline" in handler
    body = source.split("function showDeviceOffline")[1].split("\n}\n")[0]
    assert "UNAVAILABLE_TEXT" in body
    assert "setNotRunningLabel(true)" in body


def test_a_lost_connection_also_clears_the_device_panel():
    source = read("js/overview.js")
    handler = source.split('live.on("connection"')[1]
    assert "showDeviceOffline" in handler


def test_a_snapshot_with_a_stopped_simulator_shows_offline():
    source = read("js/overview.js")
    assert "running" in source
    assert "device.running" in source or "status.running" in source


def test_overview_js_device_panel_writes_text_only():
    source = read("js/overview.js")
    for risky in ("innerHTML", "eval(", "document.write", "insertAdjacentHTML"):
        assert risky not in source
