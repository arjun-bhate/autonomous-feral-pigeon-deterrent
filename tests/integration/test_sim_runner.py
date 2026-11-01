"""Tests for the async simulator runner (T029). Time comes from a FakeClock."""

import ast
import asyncio
from pathlib import Path

import pytest

from deterrent.clock import FakeClock
from deterrent.config import load_settings
from deterrent.protocol.codes import MsgType, Result, State
from deterrent.protocol.framing import Frame, FrameReader
from deterrent.protocol.messages import StatusRequest, decode_device_message, host_frame
from deterrent.simulator import runner as runner_module
from deterrent.simulator.device import Device
from deterrent.simulator.runner import SimulatorRunner


class QueueLink:
    """An in-memory link: bytes put in `to_device` arrive at the runner; its output is collected."""

    def __init__(self) -> None:
        self.to_device: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.from_device: asyncio.Queue[bytes] = asyncio.Queue()

    async def receive(self) -> bytes | None:
        return await self.to_device.get()

    async def send(self, data: bytes) -> None:
        await self.from_device.put(data)


def clock_sleep(clock: FakeClock):
    """A sleep that moves the fake clock instead of waiting, then lets other tasks run."""

    async def sleep(seconds: float) -> None:
        clock.advance(round(seconds * 1000))
        await asyncio.sleep(0)

    return sleep


async def wait_for_output(link: QueueLink, kind: int | None = None) -> Frame:
    reader = FrameReader()
    while True:
        data = await asyncio.wait_for(link.from_device.get(), timeout=5)
        for item in reader.feed(data):
            assert isinstance(item, Frame)
            if kind is None or item.type == kind:
                return item


def make_device(clock: FakeClock) -> Device:
    return Device(clock, load_settings(), allow_test_injection=True)


# ---- bytes in, bytes out ------------------------------------------------------------------------


async def test_bytes_in_produce_reply_bytes_out():
    clock = FakeClock()
    link = QueueLink()
    # A real sleep that does not move the clock keeps the device steady while we talk to it.
    runner = SimulatorRunner(make_device(clock), link, sleep=lambda s: asyncio.sleep(0.001))
    await runner.start()
    try:
        await link.to_device.put(host_frame(StatusRequest(), 1))
        frame = await wait_for_output(link, int(MsgType.STATUS_REQUEST) | 0x80)
        reply = decode_device_message(frame.type, frame.payload)
        assert reply.result is Result.OK
        assert frame.seq == 1
    finally:
        await runner.stop()


async def test_a_message_split_over_two_chunks_is_still_answered():
    clock = FakeClock()
    link = QueueLink()
    runner = SimulatorRunner(make_device(clock), link, sleep=lambda s: asyncio.sleep(0.001))
    await runner.start()
    try:
        wire = host_frame(StatusRequest(), 7)
        await link.to_device.put(wire[:3])
        await link.to_device.put(wire[3:])
        frame = await wait_for_output(link, int(MsgType.STATUS_REQUEST) | 0x80)
        assert frame.seq == 7
    finally:
        await runner.stop()


async def test_events_are_published_too():
    clock = FakeClock()
    link = QueueLink()
    runner = SimulatorRunner(make_device(clock), link, sleep=clock_sleep(clock))
    await runner.start()
    try:
        frame = await wait_for_output(link, 0xC0)  # a STATUS event, with no request sent at all
        assert frame.type == 0xC0
    finally:
        await runner.stop()


# ---- ticks --------------------------------------------------------------------------------------


class TickRecorder:
    """Stands in for the device: records when it is ticked, by fake-clock time."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.tick_times: list[int] = []
        self.received: list[bytes] = []

    def tick(self) -> list[bytes]:
        self.tick_times.append(self.clock.now_ms())
        return []

    def handle_bytes(self, data: bytes) -> list[bytes]:
        self.received.append(data)
        return [b"reply"]


async def test_ticks_fire_at_least_every_10_ms_of_fake_clock_time():
    clock = FakeClock()
    device = TickRecorder(clock)
    runner = SimulatorRunner(device, QueueLink(), sleep=clock_sleep(clock))
    await runner.start()
    while clock.now_ms() < 1000:
        await asyncio.sleep(0)
    await runner.stop()
    times = device.tick_times
    assert len(times) >= 100
    gaps = [later - earlier for earlier, later in zip(times, times[1:], strict=False)]
    assert max(gaps) <= 10


async def test_stopping_the_runner_stops_the_ticks():
    clock = FakeClock()
    device = TickRecorder(clock)
    runner = SimulatorRunner(device, QueueLink(), sleep=clock_sleep(clock))
    await runner.start()
    while len(device.tick_times) < 5:
        await asyncio.sleep(0)
    await runner.stop()
    count = len(device.tick_times)
    for _ in range(50):
        await asyncio.sleep(0)
    assert len(device.tick_times) == count


async def test_stopping_also_stops_reading_the_link():
    clock = FakeClock()
    device = TickRecorder(clock)
    link = QueueLink()
    runner = SimulatorRunner(device, link, sleep=clock_sleep(clock))
    await runner.start()
    await runner.stop()
    await link.to_device.put(b"late")
    for _ in range(20):
        await asyncio.sleep(0)
    assert device.received == []


async def test_a_tick_interval_over_10_ms_is_refused():
    with pytest.raises(ValueError):
        SimulatorRunner(TickRecorder(FakeClock()), QueueLink(), tick_interval_ms=11)
    with pytest.raises(ValueError):
        SimulatorRunner(TickRecorder(FakeClock()), QueueLink(), tick_interval_ms=0)


async def test_replies_from_the_device_are_sent_to_the_link():
    clock = FakeClock()
    device = TickRecorder(clock)
    link = QueueLink()
    runner = SimulatorRunner(device, link, sleep=lambda s: asyncio.sleep(0.001))
    await runner.start()
    try:
        await link.to_device.put(b"abc")
        assert await asyncio.wait_for(link.from_device.get(), timeout=5) == b"reply"
        assert device.received == [b"abc"]
    finally:
        await runner.stop()


async def test_the_link_closing_ends_reading_but_not_ticking():
    clock = FakeClock()
    device = TickRecorder(clock)
    link = QueueLink()
    runner = SimulatorRunner(device, link, sleep=clock_sleep(clock))
    await runner.start()
    await link.to_device.put(None)  # the link was closed
    seen = len(device.tick_times)
    while len(device.tick_times) < seen + 20:
        await asyncio.sleep(0)
    await runner.stop()


async def test_an_error_inside_the_device_is_raised_when_the_runner_stops():
    class Broken:
        def tick(self) -> list[bytes]:
            raise RuntimeError("device bug")

        def handle_bytes(self, data: bytes) -> list[bytes]:
            return []

    clock = FakeClock()
    runner = SimulatorRunner(Broken(), QueueLink(), sleep=clock_sleep(clock))
    await runner.start()
    for _ in range(10):
        await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="device bug"):
        await runner.stop()


# ---- the runner cannot change a decision --------------------------------------------------------


class StrictDevice:
    """Allows only handle_bytes and tick. Any other attribute access fails the test."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        raise AssertionError(f"the runner touched device.{name}")

    def tick(self) -> list[bytes]:
        self.calls.append("tick")
        return []

    def handle_bytes(self, data: bytes) -> list[bytes]:
        self.calls.append("handle_bytes")
        return []


async def test_the_runner_touches_only_handle_bytes_and_tick():
    clock = FakeClock()
    device = StrictDevice()
    link = QueueLink()
    runner = SimulatorRunner(device, link, sleep=clock_sleep(clock))
    await runner.start()
    await link.to_device.put(b"data")
    while "handle_bytes" not in device.calls:
        await asyncio.sleep(0)
    await runner.stop()
    assert set(device.calls) == {"tick", "handle_bytes"}


def test_the_runner_module_imports_no_decision_code():
    tree = ast.parse(Path(runner_module.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    forbidden = {
        "deterrent.simulator.arbitration",
        "deterrent.simulator.device",
        "deterrent.simulator.zones",
        "deterrent.simulator.person",
        "deterrent.simulator.control",
        "deterrent.protocol.codes",
        "deterrent.protocol.messages",
    }
    assert imported.isdisjoint(forbidden), imported & forbidden


async def test_end_to_end_with_a_real_device_state_after_homing():
    clock = FakeClock()
    device = make_device(clock)
    link = QueueLink()
    runner = SimulatorRunner(device, link, sleep=clock_sleep(clock))
    await runner.start()
    while clock.now_ms() < 3100:
        await asyncio.sleep(0)
    await runner.stop()
    assert device.state is State.DISARMED
    assert device.homed is True
