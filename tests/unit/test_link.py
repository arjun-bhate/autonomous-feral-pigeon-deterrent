"""Tests for the device links (T050): in-process link and the fault-injecting mock link."""

import ast
import asyncio
from pathlib import Path

import pytest

import deterrent.link as link_package
from deterrent.config import load_settings
from deterrent.link.inprocess import InProcessLink
from deterrent.link.mock import FaultInjectingLink
from deterrent.protocol.codes import MsgType, RejectCode, Result, State
from deterrent.protocol.framing import Frame, FrameError, FrameReader
from deterrent.protocol.messages import (
    Heartbeat,
    Jog,
    StatusRequest,
    decode_device_message,
    host_frame,
)
from deterrent.simulator.device import Device
from deterrent.simulator.events import RecordingSink
from deterrent.simulator.runner import SimulatorRunner
from support.virtual_time import VirtualTime


class Rig:
    """A real device on a virtual clock, reached through a link, with a collector for replies."""

    def __init__(self, *, mock: bool) -> None:
        self.vt = VirtualTime()
        self.sink = RecordingSink()
        self.device = Device(self.vt.clock, load_settings(), self.sink)
        self.pair = InProcessLink()
        self.host = (
            FaultInjectingLink(self.pair.host, sleep=self.vt.sleep) if mock else self.pair.host
        )
        self.runner = SimulatorRunner(self.device, self.pair.device, sleep=self.vt.sleep)
        self.received: list[Frame] = []
        self.damaged: list[FrameError] = []
        self._reader = FrameReader()
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        await self.runner.start()
        self._tasks.append(asyncio.create_task(self._collect()))
        await self.vt.advance(3100)  # BOOT and HOMING finish
        assert self.device.state is State.DISARMED
        self.received.clear()

    async def _collect(self) -> None:
        while True:
            data = await self.host.receive()
            if data is None:
                return
            for item in self._reader.feed(data):
                if isinstance(item, FrameError):
                    self.damaged.append(item)
                else:
                    self.received.append(item)

    def replies(self) -> list[Frame]:
        return [f for f in self.received if not 0xC0 <= f.type <= 0xC2]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await self.runner.stop()


@pytest.fixture
async def rig():
    r = Rig(mock=False)
    await r.start()
    yield r
    await r.stop()


@pytest.fixture
async def mock_rig():
    r = Rig(mock=True)
    await r.start()
    yield r
    await r.stop()


# ---- the in-process link ----------------------------------------------------------------------


async def test_the_in_process_link_round_trips_a_heartbeat(rig):
    await rig.host.send(host_frame(Heartbeat(True, 123), 1))
    await rig.vt.advance(20)
    (frame,) = rig.replies()
    assert frame.type == int(MsgType.HEARTBEAT) | 0x80 and frame.seq == 1
    reply = decode_device_message(frame.type, frame.payload)
    assert reply.result is Result.OK


async def test_a_closed_link_ends_receiving_with_none():
    pair = InProcessLink()
    await pair.close()
    assert await pair.host.receive() is None
    assert await pair.device.receive() is None


async def test_bytes_sent_by_one_end_come_out_of_the_other_only():
    pair = InProcessLink()
    await pair.host.send(b"to the device")
    await pair.device.send(b"to the host")
    assert await pair.device.receive() == b"to the device"
    assert await pair.host.receive() == b"to the host"


# ---- the mock link: faults on command ----------------------------------------------------------


async def test_a_pass_through_mock_link_changes_nothing(mock_rig):
    await mock_rig.host.send(host_frame(StatusRequest(), 1))
    await mock_rig.vt.advance(20)
    assert len(mock_rig.replies()) == 1


async def test_a_corrupted_frame_is_rejected_by_the_simulator_and_not_answered(mock_rig):
    mock_rig.host.to_device.corrupt_next()
    await mock_rig.host.send(host_frame(StatusRequest(), 1))
    await mock_rig.vt.advance(50)
    assert mock_rig.replies() == []
    assert sum(mock_rig.device.reject_counts.values()) == 1
    assert set(mock_rig.device.reject_counts) <= {RejectCode.BAD_CRC, RejectCode.BAD_FRAMING}
    # the very next message is fine
    await mock_rig.host.send(host_frame(StatusRequest(), 2))
    await mock_rig.vt.advance(20)
    assert len(mock_rig.replies()) == 1


async def test_a_duplicated_frame_is_answered_twice_but_acted_on_once(mock_rig):
    mock_rig.host.to_device.duplicate_next()
    await mock_rig.host.send(host_frame(Jog(100, 0), 1))
    await mock_rig.vt.advance(50)
    first, second = mock_rig.replies()
    assert first.payload == second.payload and first.seq == second.seq == 1
    assert mock_rig.device.pan == 100  # not 200: the second copy only replayed the reply
    assert len(mock_rig.sink.named("angles_changed")) == 1


async def test_a_dropped_frame_gets_no_reply_at_all(mock_rig):
    mock_rig.host.to_device.drop_next()
    await mock_rig.host.send(host_frame(StatusRequest(), 1))
    await mock_rig.vt.advance(1000)
    assert mock_rig.replies() == []
    assert mock_rig.device.reject_counts == {}  # the device never even saw it


async def test_a_dropped_reply_leaves_the_host_waiting(mock_rig):
    mock_rig.host.to_host.drop_next()
    await mock_rig.host.send(host_frame(StatusRequest(), 1))
    await mock_rig.vt.advance(1000)
    assert mock_rig.replies() == []


async def test_a_corrupted_reply_is_seen_by_the_host_as_a_damaged_frame(mock_rig):
    mock_rig.host.to_host.corrupt_next()
    await mock_rig.host.send(host_frame(StatusRequest(), 1))
    await mock_rig.vt.advance(50)
    assert mock_rig.replies() == []
    assert len(mock_rig.damaged) == 1


async def test_a_delayed_frame_arrives_only_after_the_delay(mock_rig):
    mock_rig.host.to_device.delay_next(300)
    await mock_rig.host.send(host_frame(StatusRequest(), 1))
    await mock_rig.vt.advance(299)
    assert mock_rig.replies() == []
    await mock_rig.vt.advance(10)
    assert len(mock_rig.replies()) == 1


async def test_a_later_message_can_overtake_a_delayed_one(mock_rig):
    mock_rig.host.to_device.delay_next(300)
    await mock_rig.host.send(host_frame(StatusRequest(), 1))
    await mock_rig.host.send(host_frame(StatusRequest(), 2))
    await mock_rig.vt.advance(50)
    assert [f.seq for f in mock_rig.replies()] == [2]
    await mock_rig.vt.advance(300)
    frames = mock_rig.replies()
    assert [f.seq for f in frames] == [2, 1]
    late = decode_device_message(frames[1].type, frames[1].payload)
    assert late.result is Result.REJECTED and late.code == int(RejectCode.STALE_SEQ)


async def test_reordered_frames_arrive_in_the_other_order_and_the_old_one_is_stale(mock_rig):
    mock_rig.host.to_device.reorder_next()
    await mock_rig.host.send(host_frame(StatusRequest(), 1))
    await mock_rig.host.send(host_frame(StatusRequest(), 2))
    await mock_rig.vt.advance(50)
    frames = mock_rig.replies()
    assert [f.seq for f in frames] == [2, 1]
    stale = decode_device_message(frames[1].type, frames[1].payload)
    assert stale.result is Result.REJECTED and stale.code == int(RejectCode.STALE_SEQ)


async def test_faults_apply_to_exactly_the_requested_number_of_messages(mock_rig):
    mock_rig.host.to_device.drop_next(2)
    for seq in (1, 2, 3):
        await mock_rig.host.send(host_frame(StatusRequest(), seq))
    await mock_rig.vt.advance(50)
    assert [f.seq for f in mock_rig.replies()] == [3]


# ---- the link cannot reach arbitration --------------------------------------------------------


def test_link_modules_import_nothing_from_the_simulator():
    package_dir = Path(link_package.__file__).parent
    imported: set[str] = set()
    for path in package_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
    assert not [name for name in imported if name.startswith("deterrent.simulator")]


async def test_a_link_only_moves_bytes_it_has_no_device_to_ask():
    pair = InProcessLink()
    link = FaultInjectingLink(pair.host)
    for name in ("device", "decide_fire", "arbitration", "fire", "grant"):
        assert not hasattr(link, name)
        assert not hasattr(pair.host, name)
