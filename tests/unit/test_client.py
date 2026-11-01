"""Tests for the device client (T051). Time is virtual; the device is the real simulator."""

import asyncio

import pytest

from deterrent.config import load_settings
from deterrent.link.client import CommandTimeout, DeviceClient
from deterrent.protocol.codes import DenyReason, Fault, MsgType, RejectCode, Result, State
from deterrent.protocol.framing import Frame, FrameReader
from deterrent.protocol.messages import (
    FireRequest,
    GenericReject,
    Heartbeat,
    Home,
    Reply,
    StatusEvent,
    StatusRequest,
    decode_host_message,
    device_frame,
    pack_heartbeat_data,
    unpack_granted_on_ms,
)
from support.sim_rig import OffsetClock, SimRig
from support.virtual_time import VirtualTime


@pytest.fixture
async def rig():
    r = SimRig()
    await r.start()
    yield r
    await r.stop()


def heartbeats(rig: SimRig):
    return [(at, f) for at, f in rig.sent_frames() if f.type == int(MsgType.HEARTBEAT)]


# ---- sequence numbers ----


async def test_sequence_numbers_wrap_from_65535_to_0():
    r = SimRig()
    r.client._seq = 65534  # start near the end of the 16-bit range
    await r.start(heartbeat=False, homed=False)
    for _ in range(4):
        task = asyncio.create_task(r.client.request(StatusRequest()))
        await r.vt.advance(20)
        await task
    seqs = [f.seq for _, f in r.sent_frames()]
    assert seqs == [65534, 65535, 0, 1]
    await r.stop()


async def test_each_request_gets_the_next_number(rig):
    before = len(rig.sent_frames())
    for _ in range(3):
        task = asyncio.create_task(rig.client.request(StatusRequest()))
        await rig.vt.advance(20)
        await task
    new = [f.seq for _, f in rig.sent_frames()[before:] if f.type == int(MsgType.STATUS_REQUEST)]
    assert new == sorted(new) and len(set(new)) == 3


# ---- request and reply matching ----


async def test_a_reply_is_matched_to_its_request_and_returned_decoded(rig):
    task = asyncio.create_task(rig.client.request(StatusRequest()))
    await rig.vt.advance(20)
    reply = await task
    assert isinstance(reply, Reply)
    assert reply.result is Result.OK and reply.request_type == int(MsgType.STATUS_REQUEST)


async def test_two_requests_in_flight_each_get_their_own_reply(rig):
    first = asyncio.create_task(rig.client.request(StatusRequest()))
    second = asyncio.create_task(rig.client.request(Heartbeat(True, 0)))
    await rig.vt.advance(20)
    assert (await first).request_type == int(MsgType.STATUS_REQUEST)
    assert (await second).request_type == int(MsgType.HEARTBEAT)


async def test_a_rejected_reply_is_returned_not_raised(rig):
    task = asyncio.create_task(rig.client.request(Home()))  # HOME is allowed when DISARMED
    await rig.vt.advance(20)
    assert (await task).result is Result.OK
    task = asyncio.create_task(rig.client.request(Home()))  # but not while already homing
    await rig.vt.advance(20)
    reply = await task
    assert reply.result is Result.REJECTED and reply.code == int(RejectCode.BUSY_HOMING)


# ---- timeouts ----


async def test_no_reply_within_command_ttl_raises_a_timeout():
    r = SimRig(mock=True)
    await r.start()
    r.faults.to_host.drop_next()
    task = asyncio.create_task(r.client.request(StatusRequest()))
    await r.vt.advance(499)
    assert not task.done()
    await r.vt.advance(1)
    with pytest.raises(CommandTimeout) as caught:
        await task
    assert caught.value.msg_type == int(MsgType.STATUS_REQUEST)
    await r.stop()


async def test_a_late_reply_after_a_timeout_is_ignored_and_counted():
    r = SimRig(mock=True)
    await r.start()
    r.faults.to_host.delay_next(700)
    task = asyncio.create_task(r.client.request(StatusRequest()))
    await r.vt.advance(500)
    with pytest.raises(CommandTimeout):
        await task
    before = r.client.unmatched_replies
    await r.vt.advance(300)
    assert r.client.unmatched_replies == before + 1
    await r.stop()


async def test_a_damaged_reply_is_counted_and_the_request_times_out():
    r = SimRig(mock=True)
    await r.start()
    r.faults.to_host.corrupt_next()
    task = asyncio.create_task(r.client.request(StatusRequest()))
    await r.vt.advance(500)
    with pytest.raises(CommandTimeout):
        await task
    assert r.client.damaged_frames == 1
    await r.stop()


async def test_requests_before_start_are_refused():
    r = SimRig()
    with pytest.raises(RuntimeError):
        await r.client.request(StatusRequest())


# ---- heartbeat ----


async def test_a_heartbeat_is_sent_every_200_ms(rig):
    start = rig.vt.elapsed_ms
    await rig.vt.advance(1000)
    times = [at for at, _ in heartbeats(rig) if at >= start]
    gaps = {later - earlier for earlier, later in zip(times, times[1:], strict=False)}
    assert gaps == {200}
    assert len(times) in (5, 6)


async def test_the_heartbeat_carries_the_person_clear_provider_value():
    state = {"clear": True}
    r = SimRig(person_clear=lambda: state["clear"])
    await r.start()
    state["clear"] = False
    await r.vt.advance(400)
    last = decode_host_message(*[(f.type, f.payload) for _, f in heartbeats(r)][-1])
    assert isinstance(last, Heartbeat) and last.person_clear is False
    state["clear"] = True
    await r.vt.advance(400)
    last = decode_host_message(*[(f.type, f.payload) for _, f in heartbeats(r)][-1])
    assert last.person_clear is True
    await r.stop()


async def test_person_clear_defaults_to_clear():
    r = SimRig()
    await r.start()
    first = decode_host_message(*[(f.type, f.payload) for _, f in heartbeats(r)][0])
    assert first.person_clear is True
    await r.stop()


async def test_the_simulator_stays_out_of_safe_error_while_heartbeats_flow(rig):
    await rig.vt.advance(5000)
    assert rig.device.state is State.DISARMED
    assert rig.device.fault_mask == 0


async def test_stopping_the_heartbeat_source_sends_the_simulator_to_safe_error():
    r = SimRig()
    await r.start()
    await r.vt.advance(1000)
    assert r.device.state is State.DISARMED
    await r.client.stop()  # no more heartbeats
    await r.vt.advance(r.settings.watchdog_ms - 100)
    assert r.device.state is State.DISARMED
    await r.vt.advance(300)
    assert r.device.state is State.SAFE_ERROR
    assert r.device.fault_mask & Fault.HEARTBEAT_LOST
    await r.runner.stop()


async def test_a_slow_heartbeat_reply_does_not_delay_the_next_heartbeat():
    r = SimRig(mock=True)
    await r.start()
    start = r.vt.elapsed_ms
    r.faults.to_host.drop_next(3)  # three heartbeat replies lost
    await r.vt.advance(1000)
    times = [at for at, _ in heartbeats(r) if at >= start]
    assert {b - a for a, b in zip(times, times[1:], strict=False)} == {200}
    await r.stop()


# ---- device time ----


async def test_issued_at_uses_the_device_time_base_not_the_laptop_clock():
    r = SimRig(laptop_offset_ms=5_000_000)  # the laptop clock reads 5000 s ahead of the device
    await r.start()
    assert r.client.time_offset_ms is not None
    task = asyncio.create_task(
        r.client.fire_request(intended_pan=100, intended_tilt=100, duration_ms=500)
    )
    await r.vt.advance(20)
    await task
    fires = [f for _, f in r.sent_frames() if f.type == int(MsgType.FIRE_REQUEST)]
    message = decode_host_message(fires[0].type, fires[0].payload)
    assert isinstance(message, FireRequest)
    assert abs(message.issued_at_ms - r.device.now_ms()) <= 20
    assert r.laptop_clock.now_ms() - message.issued_at_ms > 4_000_000  # clearly not laptop time
    await r.stop()


async def test_the_offset_is_learned_from_the_heartbeat_reply():
    r = SimRig(laptop_offset_ms=1234)
    await r.start()
    await r.vt.advance(400)
    assert r.client.time_offset_ms is not None
    assert abs(r.client.device_time_ms() - r.device.now_ms()) <= 1
    await r.stop()


async def test_a_fire_request_ttl_defaults_to_the_setting_and_can_be_given(rig):
    for ttl in (None, 300):
        task = asyncio.create_task(
            rig.client.fire_request(0, 0, 500, **({} if ttl is None else {"ttl_ms": ttl}))
        )
        await rig.vt.advance(20)
        await task
    fires = [f for _, f in rig.sent_frames() if f.type == int(MsgType.FIRE_REQUEST)]
    ttls = [decode_host_message(f.type, f.payload).ttl_ms for f in fires]
    assert ttls == [load_settings().fire_ttl_ms, 300]


# ---- the client cannot decide or fake a grant ----


class ScriptedLink:
    """A fake device end: records what the client sends and plays back replies the test chooses."""

    def __init__(self) -> None:
        self.sent: list[Frame] = []
        self._inbox: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._reader = FrameReader()

    async def send(self, data: bytes) -> None:
        for item in self._reader.feed(data):
            assert isinstance(item, Frame)
            self.sent.append(item)

    async def receive(self) -> bytes | None:
        return await self._inbox.get()

    async def reply(self, message, seq: int) -> None:
        await self._inbox.put(device_frame(message, seq))


async def make_scripted(offset: int | None = 0):
    vt = VirtualTime()
    link = ScriptedLink()
    client = DeviceClient(
        link, OffsetClock(vt.clock, 0), load_settings(), sleep=vt.sleep, person_clear=lambda: True
    )
    await client.start(heartbeat=False)
    client._offset_ms = offset  # known offset, so no clock sync is attempted
    return vt, link, client


async def test_a_denied_reply_stays_denied_whatever_else_it_carries():
    vt, link, client = await make_scripted()
    task = asyncio.create_task(client.fire_request(0, 0, 500))
    await vt.advance(1)
    # the device says DENIED, but with reply data shaped like a grant (a "granted_on_ms" value)
    lying = Reply(
        int(MsgType.FIRE_REQUEST), Result.DENIED, int(DenyReason.KEY_OFF), b"\x01\x02\x03\x04"
    )
    await link.reply(lying, link.sent[-1].seq)
    await vt.advance(1)
    reply = await task
    assert reply.result is Result.DENIED
    assert reply.code == int(DenyReason.KEY_OFF)
    assert reply is not None and unpack_granted_on_ms(reply.data) == 0x04030201  # data untouched
    await client.stop()


async def test_a_granted_reply_is_returned_exactly_as_the_device_sent_it():
    vt, link, client = await make_scripted()
    task = asyncio.create_task(client.fire_request(0, 0, 500))
    await vt.advance(1)
    sent = Reply.granted(777)
    await link.reply(sent, link.sent[-1].seq)
    await vt.advance(1)
    assert await task == sent
    await client.stop()


async def test_a_reply_of_the_wrong_type_does_not_answer_the_request():
    vt, link, client = await make_scripted()
    task = asyncio.create_task(client.request(StatusRequest()))
    await vt.advance(1)
    await link.reply(
        Reply.granted(1), link.sent[-1].seq
    )  # a FIRE_REQUEST reply to a STATUS request
    await vt.advance(600)
    with pytest.raises(CommandTimeout):
        await task
    assert client.unmatched_replies == 1
    await client.stop()


async def test_a_generic_reject_for_an_unknown_type_answers_the_request():
    vt, link, client = await make_scripted()
    task = asyncio.create_task(client.request(StatusRequest()))
    await vt.advance(1)
    await link.reply(GenericReject(0x99), link.sent[-1].seq)
    await vt.advance(1)
    assert isinstance(await task, GenericReject)
    await client.stop()


async def test_the_client_never_answers_a_fire_request_without_a_device_reply():
    vt, link, client = await make_scripted()
    task = asyncio.create_task(client.fire_request(0, 0, 500))
    await vt.advance(2000)  # the device says nothing
    with pytest.raises(CommandTimeout):
        await task
    await client.stop()


async def test_the_client_has_no_grant_or_decision_api():
    for name in ("grant", "decide", "decide_fire", "approve", "allow", "force"):
        assert not hasattr(DeviceClient, name)


async def test_unsolicited_events_go_to_the_event_callback():
    events = []
    vt = VirtualTime()
    link = ScriptedLink()
    client = DeviceClient(
        link,
        OffsetClock(vt.clock, 0),
        load_settings(),
        sleep=vt.sleep,
        on_event=events.append,
    )
    await client.start(heartbeat=False)
    from deterrent.protocol.messages import StatusBlock

    block = StatusBlock(State.DISARMED, 1, 2, True, False, True, True, False, 0, 0)
    await link.reply(StatusEvent(block), 5)
    await vt.advance(1)
    assert len(events) == 1 and isinstance(events[0], StatusEvent)
    assert events[0].status == block
    await client.stop()


async def test_a_heartbeat_reply_with_a_bad_data_length_does_not_set_the_offset():
    vt, link, client = await make_scripted(None)
    task = asyncio.create_task(client.request(Heartbeat(True, 0)))
    await vt.advance(1)
    await link.reply(Reply.ok(int(MsgType.HEARTBEAT), b"\x01"), link.sent[-1].seq)
    await vt.advance(1)
    await task
    assert client.time_offset_ms is None
    await client.stop()


async def test_a_heartbeat_reply_sets_the_offset():
    vt, link, client = await make_scripted(None)
    task = asyncio.create_task(client.request(Heartbeat(True, 0)))
    await vt.advance(100)
    await link.reply(
        Reply.ok(int(MsgType.HEARTBEAT), pack_heartbeat_data(9_000, State.DISARMED)),
        link.sent[-1].seq,
    )
    await vt.advance(1)
    await task
    assert client.time_offset_ms is not None
    assert abs(client.device_time_ms() - 9_000) <= 100
    await client.stop()


async def test_without_a_known_offset_a_fire_request_first_tries_to_sync_the_clock():
    vt, link, client = await make_scripted(None)
    task = asyncio.create_task(client.fire_request(0, 0, 500))
    await vt.advance(1)
    assert link.sent[0].type == int(MsgType.HEARTBEAT)  # the sync attempt comes first
    await vt.advance(600)  # no answer to it
    await vt.advance(1)
    assert [f.type for f in link.sent][-1] == int(MsgType.FIRE_REQUEST)  # then it goes ahead anyway
    await link.reply(Reply.denied(DenyReason.NOT_HOMED), link.sent[-1].seq)
    await vt.advance(1)
    assert (await task).result is Result.DENIED
    await client.stop()
