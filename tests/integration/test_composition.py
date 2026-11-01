"""Tests for composition.py (T052): the wiring of simulator, link, client, storage and logging."""

import asyncio

import pytest

from deterrent.composition import Platform
from deterrent.config import load_settings
from deterrent.link.client import CommandTimeout
from deterrent.link.mock import FaultInjectingLink
from deterrent.observability import queries
from deterrent.protocol.codes import DenyReason, Result
from deterrent.protocol.messages import StatusRequest
from support.virtual_time import VirtualTime

WALL_START_US = 1_700_000_000_000_000


class Env:
    def __init__(self, tmp_path, *, faults: bool = False) -> None:
        self.vt = VirtualTime()
        wrapper = None
        self.faults: FaultInjectingLink | None = None
        if faults:

            def wrapper(link):
                self.faults = FaultInjectingLink(link, sleep=self.vt.sleep)
                return self.faults

        self.platform = Platform(
            load_settings(),
            data_dir=tmp_path,
            clock=self.vt.clock,
            wall_clock_us=lambda: WALL_START_US + self.vt.elapsed_ms * 1000,
            sleep=self.vt.sleep,
            start_store_thread=False,
            boot_duration_ms=0,
            link_wrapper=wrapper,
        )

    async def start(self, *, simulator: bool = True, homed: bool = True) -> None:
        await self.platform.start()
        if simulator:
            task = asyncio.create_task(self.platform.start_simulator())
            await self.vt.advance(50)
            await task
            if homed:
                await self.vt.advance(3100)

    async def stop(self) -> None:
        await self.platform.stop()

    def query(self):
        self.platform.store.flush()
        return self.platform.store.open_read_connection()

    def counter(self, name: str, **labels) -> float:
        conn = self.query()
        try:
            total = 0.0
            for c in queries.metric_summary(conn).counters:
                if c.name == name and c.labels == labels:
                    total += c.total
            return total
        finally:
            conn.close()

    async def send(self, coro_factory, ms: int = 50):
        task = asyncio.create_task(coro_factory())
        await self.vt.advance(ms)
        return await task

    async def fire(self, **kw):
        return await self.send(lambda: self.platform.client.fire_request(0, 0, 500, **kw))

    async def arm_and_wait_ready(self) -> None:
        self.platform.set_key(True)
        reply = await self.send(
            lambda: self.platform.client.request(
                __import__("deterrent.protocol.messages", fromlist=["Arm"]).Arm()
            )
        )
        assert reply.result is Result.OK
        await self.vt.advance(6000)  # the person re-engage delay (5 s) has passed


@pytest.fixture
async def env(tmp_path):
    e = Env(tmp_path)
    await e.start()
    yield e
    await e.stop()


async def test_building_the_app_graph_starts_and_stops_cleanly(tmp_path):
    e = Env(tmp_path)
    await e.start(simulator=False)
    assert e.platform.simulator_running is False
    task = asyncio.create_task(e.platform.start_simulator())
    await e.vt.advance(50)
    await task
    assert e.platform.simulator_running is True
    await e.stop()
    assert e.platform.simulator_running is False
    assert e.platform.store_closed is True


async def test_settings_come_from_the_config_file(env):
    assert env.platform.settings.cooldown_ms == 5000
    assert env.platform.settings.heartbeat_interval_ms == 200


async def test_data_files_are_in_separate_places(env, tmp_path):
    env.platform.store.flush()
    assert (tmp_path / "observability.sqlite3").exists()
    assert (tmp_path / "device" / "zone_store.sqlite3").exists()


async def test_starting_twice_and_stopping_when_stopped_are_safe(env):
    with pytest.raises(RuntimeError):
        await env.platform.start_simulator()
    await env.platform.stop_simulator()
    await env.platform.stop_simulator()  # no error
    assert env.platform.simulator_running is False


async def test_a_denied_fire_request_counts_one_denial_for_that_reason(env):
    assert env.counter("fire_requests_denied", reason="KEY_OFF") == 0
    reply = await env.fire()
    assert reply.result is Result.DENIED and reply.code == int(DenyReason.KEY_OFF)
    assert env.counter("fire_requests_denied", reason="KEY_OFF") == 1
    await env.fire()
    assert env.counter("fire_requests_denied", reason="KEY_OFF") == 2
    assert env.counter("fire_requests_granted") == 0


async def test_a_granted_fire_request_counts_one_grant(env):
    await env.arm_and_wait_ready()
    reply = await env.fire()
    assert reply.result is Result.GRANTED
    assert env.counter("fire_requests_granted") == 1
    assert env.counter("fire_requests_denied", reason="KEY_OFF") == 0


async def test_a_cut_counts_an_interlock_event_by_cause(env):
    await env.arm_and_wait_ready()
    reply = await env.send(lambda: env.platform.client.fire_request(0, 0, 5000))
    assert reply.result is Result.GRANTED
    env.platform.set_person_clear(False)  # a person appears while the shot runs
    await env.vt.advance(1500)
    assert env.counter("interlock_events", cause="PERSON_NOT_CLEAR") == 1
    conn = env.query()
    messages = [i.message for i in queries.query_logs(conn, component="simulator").items]
    conn.close()
    assert any("cut" in m.lower() for m in messages)


async def test_a_corrupted_frame_counts_a_rejected_message_by_cause(tmp_path):
    e = Env(tmp_path, faults=True)
    await e.start()
    e.faults.to_device.corrupt_next()
    with pytest.raises(CommandTimeout):
        await e.send(lambda: e.platform.client.request(StatusRequest()), ms=600)
    rejected = e.counter("protocol_messages_rejected", cause="BAD_CRC") + e.counter(
        "protocol_messages_rejected", cause="BAD_FRAMING"
    )
    assert rejected == 1
    await e.stop()


async def test_a_rejected_message_with_a_reply_is_counted_too(env):
    from deterrent.protocol.messages import Move

    reply = await env.send(lambda: env.platform.client.request(Move(30000, 0)))
    assert reply.result is Result.REJECTED
    assert env.counter("protocol_messages_rejected", cause="OUT_OF_SOFT_LIMIT") == 1


async def test_each_event_writes_a_structured_log_entry(env):
    await env.fire()  # denied
    await env.arm_and_wait_ready()
    await env.fire()  # granted
    conn = env.query()
    items = queries.query_logs(conn, component="simulator", limit=1000).items
    conn.close()
    text = " | ".join(i.message for i in items).lower()
    for expected in ("state", "denied", "granted", "effector", "homing"):
        assert expected in text, expected
    denied = next(i for i in items if "denied" in i.message.lower())
    assert denied.fields["reason"] == "KEY_OFF" and "device_ms" in denied.fields
    assert all(i.component == "simulator" for i in items)


async def test_homing_drift_is_recorded_as_a_sample_in_degrees(env):
    conn = env.query()
    dist = queries.metric_series(conn, "homing_drift_simulated")
    conn.close()
    assert dist.unit == "degrees" and len(dist.points) == 1
    assert dist.points[0][1] == pytest.approx(0.03)


async def test_the_live_status_is_kept_in_memory_and_follows_the_device(env):
    status = env.platform.device_status()
    assert status["running"] is True and status["state"] == "DISARMED"
    for key in (
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
    ):
        assert key in status
    from deterrent.protocol.messages import Move

    await env.send(lambda: env.platform.client.request(Move(6000, 2000)))
    await env.vt.advance(20)
    status = env.platform.device_status()
    assert (status["pan_cdeg"], status["tilt_cdeg"]) == (6000, 2000)


async def test_the_status_with_the_simulator_stopped_is_exactly_not_running(env):
    await env.platform.stop_simulator()
    assert env.platform.device_status() == {"running": False}


async def test_device_listeners_hear_updates_and_the_stop(env):
    heard = []
    env.platform.add_device_listener(heard.append)
    await env.vt.advance(1100)
    assert heard and heard[-1]["running"] is True
    await env.platform.stop_simulator()
    assert heard[-1] is None


async def test_the_person_clear_value_reaches_the_heartbeat(env):
    env.platform.set_person_clear(False)
    await env.vt.advance(400)
    assert env.platform.device.person_snapshot().person_clear is False
    env.platform.set_person_clear(True)
    await env.vt.advance(400)
    assert env.platform.device.person_snapshot().person_clear is True


async def test_storage_fault_hook_raises_and_clears_the_warning(env):
    env.platform.set_storage_mode("locked")
    env.platform.logger.info("api", "write that will fail")
    env.platform.store.pump()
    assert env.platform.storage_warning is not None
    env.platform.set_storage_mode("ok")
    await env.vt.advance(2100)
    env.platform.store.pump()
    assert env.platform.storage_warning is None


async def test_the_zone_store_can_be_corrupted_only_when_a_zone_exists(env):
    with pytest.raises(ValueError):
        env.platform.corrupt_zone_store()
