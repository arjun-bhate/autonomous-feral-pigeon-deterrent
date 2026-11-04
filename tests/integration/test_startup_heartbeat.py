"""The laptop must not send heartbeats while the device is still in BOOT."""

import asyncio

from deterrent.observability import queries
from deterrent.protocol.codes import State
from support.platform_env import Env

BOOT_MS = 1000


async def _start_booting(tmp_path):
    e = Env(tmp_path, boot_duration_ms=BOOT_MS)
    await e.start(simulator=False)
    task = asyncio.create_task(e.platform.start_simulator())
    await e.vt.advance(20)
    await task
    return e


def _rejections(e) -> float:
    return e.counter("protocol_messages_rejected", cause="WRONG_STATE")


def _warnings(e) -> list[str]:
    conn = e.query()
    try:
        return [
            i.message
            for i in queries.query_logs(conn, component="simulator").items
            if i.severity == "WARNING"
        ]
    finally:
        conn.close()


async def test_a_fresh_simulator_rejects_nothing_before_homing(tmp_path):
    e = await _start_booting(tmp_path)
    try:
        await e.vt.advance(BOOT_MS - 100)  # still in BOOT
        assert e.platform.device.state is State.BOOT
        assert _rejections(e) == 0
        await e.vt.advance(300)  # BOOT -> HOMING
        assert e.platform.device.state is State.HOMING
        assert _rejections(e) == 0
        assert _warnings(e) == []
    finally:
        await e.stop()


async def test_heartbeats_begin_within_one_interval_after_boot_ends(tmp_path):
    e = await _start_booting(tmp_path)
    try:
        await e.vt.advance(BOOT_MS + 20)  # BOOT has just ended
        assert e.platform.device.state is State.HOMING
        await e.vt.advance(e.platform.settings.heartbeat_interval_ms)
        assert e.platform.device._heartbeat_seen is True  # the watchdog has started
        assert _rejections(e) == 0
    finally:
        await e.stop()
