"""Restart persistence and storage faults (T071).

Three promises are tested here, end to end, on the real wired program (``Platform``) with
virtual time:

1. **Restart**: logs, traces, metrics and the simulated device's zones are still there
   after everything is rebuilt on the same folders. The laptop side never opens the
   device's ``zone_store.sqlite3`` file; only the simulator's zone storage does.
2. **Storage fault** (disk full, database locked): the live WebSocket keeps going, a
   warning appears within 10 seconds, nothing crashes, and the simulated device's safety
   decisions are exactly the same as on a healthy disk.
3. **Zone storage fault**: when the device's zone storage is full or locked, a zone upload
   is rejected and the zones stored before are still there, untouched.

In these tests the observability store has no background writer thread (the tests control
time), so ``store.pump()`` stands in for one writer pass. The real thread is exercised in
the T075 server run.
"""

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

from deterrent.observability import queries
from deterrent.protocol.codes import DenyReason, RejectCode, Result
from deterrent.protocol.crc import zone_crc32
from deterrent.protocol.messages import (
    StatusBlock,
    StatusRequest,
    ZoneBegin,
    ZoneCommit,
    ZoneList,
    ZonePoint,
    unpack_zone_list,
)
from deterrent.simulator.zone_storage import StorageError
from support.api_env import api_env
from support.fake_socket import FakeSocket
from support.platform_env import Env

SQUARE = [(0, 0), (1000, 0), (1000, 1000), (0, 1000)]
OTHER_SQUARE = [(2000, 0), (3000, 0), (3000, 1000), (2000, 1000)]
ZONE_FILE = "zone_store.sqlite3"


# ---- helpers -----------------------------------------------------------------------------------


async def upload_zone(env: Env, zone_id: int, points):
    """Send a whole zone upload through the normal client.

    Returns the reply that ended it: the first message the device did not accept (a refused
    upload stops there), or the commit reply when every step was accepted.
    """
    client = env.platform.client
    steps = [lambda: client.request(ZoneBegin(zone_id, len(points)))]
    for index, (pan, tilt) in enumerate(points):
        steps.append(lambda i=index, p=pan, t=tilt: client.request(ZonePoint(i, p, t)))
    steps.append(lambda: client.request(ZoneCommit(zone_crc32(zone_id, points))))
    for step in steps:
        reply = await env.send(step)
        if reply.result is not Result.OK:
            return reply
    return reply


async def listed_zones(env: Env) -> list[int]:
    reply = await env.send(lambda: env.platform.client.request(ZoneList()))
    assert reply.result is Result.OK
    return [zone.zone_id for zone in unpack_zone_list(reply.data)]


async def status_block(env: Env) -> StatusBlock:
    reply = await env.send(lambda: env.platform.client.request(StatusRequest()))
    block = StatusBlock.decode_payload(reply.data)
    assert isinstance(block, StatusBlock)
    return block


async def engage(env: Env, **body) -> str:
    response = await env.send(
        lambda: env.client.post("/api/dev/engagement", json={"fire": True, **body}), 150
    )
    assert response.status_code == 200
    return response.json()["trace_id"]


# ---- 1. restart --------------------------------------------------------------------------------


async def test_logs_traces_metrics_and_zones_survive_a_full_rebuild(tmp_path):
    first = Env(tmp_path)
    await first.start()
    first.platform.logger.info("api", "written before the restart")
    reply = await upload_zone(first, 3, SQUARE)
    assert reply.result is Result.OK
    trace_id = await first.send(lambda: first.platform.engagement.run(fire=True), 150)
    assert first.counter("fire_requests_denied", reason="KEY_OFF") == 1
    await first.stop()

    second = Env(tmp_path)  # a new Platform, store and device on the same folders
    await second.start()
    try:
        conn = second.query()
        try:
            messages = [r.message for r in queries.query_logs(conn, limit=1000).items]
            assert "written before the restart" in messages
            assert queries.get_trace(conn, trace_id) is not None
        finally:
            conn.close()
        assert second.counter("fire_requests_denied", reason="KEY_OFF") == 1  # not reset
        assert await listed_zones(second) == [3]
        assert (await status_block(second)).zone_store_valid is True
    finally:
        await second.stop()


async def test_log_ids_keep_going_up_after_a_restart(tmp_path):
    first = Env(tmp_path)
    await first.start(simulator=False)
    first.platform.logger.info("api", "one")
    highest = first.platform.logger.info("api", "two")
    await first.stop()
    second = Env(tmp_path)
    await second.start(simulator=False)
    try:
        newest = second.platform.logger.info("api", "three")
        assert newest.id > highest.id
    finally:
        await second.stop()


class OpenedFiles:
    """Records every sqlite3.connect made by code in the application, by calling file."""

    def __init__(self, monkeypatch) -> None:
        self.opened: list[tuple[str, str]] = []
        real_connect = sqlite3.connect

        def watching(database, *args, **kwargs):
            caller = Path(sys._getframe(1).f_code.co_filename).as_posix()
            self.opened.append((caller, str(database)))
            return real_connect(database, *args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", watching)

    def openers_of(self, file_name: str) -> set[str]:
        return {
            caller.split("src/")[-1]
            for caller, database in self.opened
            if file_name in database and "/src/deterrent/" in caller
        }


async def test_only_the_simulators_zone_storage_ever_opens_the_zone_store_file(
    tmp_path, monkeypatch
):
    watcher = OpenedFiles(monkeypatch)
    async with api_env(tmp_path) as env:
        await upload_zone(env, 1, SQUARE)
        await engage(env)
        env.platform.store.flush()
        for path in ("/api/logs", "/api/traces", "/api/metrics", "/api/device/status"):
            assert (await env.client.get(path)).status_code == 200
    assert watcher.openers_of(ZONE_FILE) == {"deterrent/simulator/zone_storage.py"}
    assert watcher.openers_of("observability.sqlite3") <= {"deterrent/observability/store.py"}
    assert watcher.openers_of("observability.sqlite3")  # the check is not empty


async def test_the_zone_store_and_the_log_database_are_separate_files(tmp_path):
    async with api_env(tmp_path) as env:
        await upload_zone(env, 1, SQUARE)
        env.platform.store.flush()
        assert (tmp_path / "device" / ZONE_FILE).is_file()
        assert (tmp_path / "observability.sqlite3").is_file()
        assert (tmp_path / "device" / ZONE_FILE) != (tmp_path / "observability.sqlite3")
        # the log database has no zone table and the zone database has no log table
        logs = sqlite3.connect(tmp_path / "observability.sqlite3")
        zones = sqlite3.connect(tmp_path / "device" / ZONE_FILE)
        try:
            log_tables = {r[0] for r in logs.execute("SELECT name FROM sqlite_master")}
            zone_tables = {r[0] for r in zones.execute("SELECT name FROM sqlite_master")}
        finally:
            logs.close()
            zones.close()
        assert "zone" not in log_tables and "logs" not in zone_tables


# ---- 2. a failing log disk ---------------------------------------------------------------------


async def safety_script(env: Env) -> list[tuple[str, int]]:
    """A fixed series of fire requests; the answers are the device's safety decisions."""
    answers = []

    async def ask():
        reply = await env.fire()
        answers.append((reply.result.name, reply.code))

    await ask()  # key off
    await env.arm_and_wait_ready()
    await ask()  # granted
    await ask()  # a shot is already running
    await env.vt.advance(700)
    await ask()  # cooldown
    env.platform.set_person_clear(False)
    await env.vt.advance(1200)
    await ask()  # a person is in view
    return answers


@pytest.mark.parametrize("mode", ["full", "locked"])
async def test_safety_decisions_are_identical_with_a_failing_log_disk(tmp_path, mode):
    healthy = Env(tmp_path / "healthy")
    await healthy.start()
    expected = await safety_script(healthy)
    await healthy.stop()

    failing = Env(tmp_path / "failing")
    await failing.start()
    failing.platform.set_storage_mode(mode)
    actual = await safety_script(failing)
    failing.platform.store.pump()
    assert failing.platform.storage_warning is not None  # the fault really was active
    await failing.stop()

    assert actual == expected
    assert expected[0] == ("DENIED", int(DenyReason.KEY_OFF))
    assert expected[1][0] == "GRANTED"
    assert expected[2][0] == "REJECTED"
    assert expected[3][0] == "DENIED"
    assert expected[4][0] == "DENIED"


@pytest.mark.parametrize("mode", ["full", "locked"])
async def test_the_live_channel_keeps_going_and_a_warning_appears_within_ten_seconds(
    tmp_path, mode
):
    async with api_env(tmp_path, sensors=True) as env:
        socket = FakeSocket()
        serving = asyncio.create_task(env.app.state.hub.serve(socket))
        await env.vt.advance(1500)
        assert (
            await env.client.post("/api/dev/storage/fail", json={"mode": mode})
        ).status_code == 200
        env.platform.logger.info("api", "a line that cannot be saved")
        before = len(socket.sent)

        seconds_to_warning = None
        for second in range(1, 11):  # one writer pass per second
            await env.vt.advance(1000)
            env.platform.store.pump()
            status = (await env.client.get("/api/system/status")).json()
            if status["storage_warning"] is not None:
                seconds_to_warning = second
                break
        assert seconds_to_warning is not None and seconds_to_warning <= 10

        await env.vt.advance(6000)  # the channel keeps sending while the disk is failing
        kinds = [m["type"] for m in socket.sent[before:]]
        assert kinds.count("tick") >= 5
        assert kinds.count("health") >= 5
        assert "storage_warning" in kinds
        assert socket.of_type("storage_warning")[0]["message"]

        # still answering, still the same device, no crash
        assert (await env.client.get("/api/health/current")).status_code == 200
        assert (await env.client.get("/api/device/status")).json()["running"] is True
        assert env.platform.simulator_running and env.app.state.hub.client_count == 1
        assert env.platform.store.last_internal_error is None
        assert (await engage(env)) is not None  # an engagement still runs and finishes

        # and when the disk recovers the warning clears and the held lines are saved
        assert (
            await env.client.post("/api/dev/storage/fail", json={"mode": "ok"})
        ).status_code == 200
        await env.vt.advance(3000)
        env.platform.store.pump()
        assert (await env.client.get("/api/system/status")).json()["storage_warning"] is None
        env.platform.store.flush()
        messages = [r["message"] for r in (await env.client.get("/api/logs")).json()["items"]]
        assert "a line that cannot be saved" in messages

        socket.disconnect()
        await serving


# ---- 3. a failing zone storage on the device ----------------------------------------------------


async def zones_on_disk(env: Env) -> list[tuple]:
    storage = env.platform.device.storage
    return [(z.zone_id, z.crc32, z.point_count, z.points) for z in storage.read_all()]


async def test_a_locked_zone_storage_rejects_the_upload_and_keeps_the_old_zones(tmp_path):
    async with api_env(tmp_path) as env:
        assert (await upload_zone(env, 1, SQUARE)).result is Result.OK
        before = await zones_on_disk(env)
        assert len(before) == 1

        locker = sqlite3.connect(tmp_path / "device" / ZONE_FILE, isolation_level=None)
        locker.execute("BEGIN EXCLUSIVE")  # another program holds the database
        try:
            reply = await upload_zone(env, 2, OTHER_SQUARE)
        finally:
            locker.execute("ROLLBACK")
            locker.close()

        assert reply.result is Result.REJECTED
        assert reply.code in (int(RejectCode.ZONE_STORAGE_ERROR), int(RejectCode.ZONE_STORE_FULL))
        assert await zones_on_disk(env) == before
        assert await listed_zones(env) == [1]
        assert (await status_block(env)).zone_store_valid is True
        # once the lock is gone, the same upload works
        assert (await upload_zone(env, 2, OTHER_SQUARE)).result is Result.OK
        assert await listed_zones(env) == [1, 2]


async def test_a_full_zone_storage_rejects_the_upload_and_keeps_the_old_zones(
    tmp_path, monkeypatch
):
    async with api_env(tmp_path) as env:
        assert (await upload_zone(env, 1, SQUARE)).result is Result.OK
        before = await zones_on_disk(env)
        storage = env.platform.device.storage

        def disk_full(zone):
            raise StorageError("database or disk is full")

        monkeypatch.setattr(storage, "write_zone", disk_full)
        reply = await upload_zone(env, 2, OTHER_SQUARE)
        monkeypatch.undo()

        assert reply.result is Result.REJECTED
        assert reply.code == int(RejectCode.ZONE_STORAGE_ERROR)
        assert await zones_on_disk(env) == before
        assert await listed_zones(env) == [1]
        assert (await status_block(env)).zone_store_valid is True
        assert (await upload_zone(env, 2, OTHER_SQUARE)).result is Result.OK
