"""Test helper: a Platform (the wired program) running on virtual time. Not a test file."""

import asyncio

from deterrent.composition import Platform
from deterrent.config import Settings, load_settings
from deterrent.health.sensors import SENSOR_UNITS, MockSensorProvider
from deterrent.link.mock import FaultInjectingLink
from deterrent.observability import queries
from deterrent.protocol.codes import Result
from deterrent.protocol.messages import Arm
from support.sensor_spawn import spawn_and_wait_briefly
from support.virtual_time import VirtualTime

WALL_START_US = 1_700_000_000_000_000


class Env:
    """A started platform whose clocks are all one VirtualTime."""

    def __init__(
        self,
        tmp_path,
        *,
        faults: bool = False,
        dev_tools: bool = True,
        sensors: bool = False,
        boot_duration_ms: int = 0,
    ) -> None:
        self.vt = VirtualTime()
        wrapper = None
        self.faults: FaultInjectingLink | None = None
        if faults:

            def wrapper(link):
                self.faults = FaultInjectingLink(link, sleep=self.vt.sleep)
                return self.faults

        # Mock sensors with distinct values; with sensors=False no health sensors run at all.
        self.sensors = (
            {name: MockSensorProvider(name, 10.0 + i) for i, name in enumerate(SENSOR_UNITS)}
            if sensors
            else {}
        )
        self.platform = Platform(
            Settings(**{**load_settings().model_dump(), "dev_tools": dev_tools}),
            data_dir=tmp_path,
            clock=self.vt.clock,
            wall_clock_us=lambda: WALL_START_US + self.vt.elapsed_ms * 1000,
            sleep=self.vt.sleep,
            monotonic_us=lambda: self.vt.elapsed_ms * 1000,
            start_store_thread=False,
            boot_duration_ms=boot_duration_ms,
            link_wrapper=wrapper,
            sensor_providers=list(self.sensors.values()),
            health_spawn=spawn_and_wait_briefly,
        )

    async def start(self, *, simulator: bool = True, homed: bool = True) -> None:
        if not self.platform.started:
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
        reply = await self.send(lambda: self.platform.client.request(Arm()))
        assert reply.result is Result.OK
        await self.vt.advance(6000)  # the person re-engage delay (5 s) has passed
