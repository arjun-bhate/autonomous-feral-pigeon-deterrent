"""Tests for boot, homing and status (T016)."""

from deterrent.protocol.codes import EventType, MsgType, Result, State
from deterrent.protocol.messages import StatusBlock, StatusEvent, StatusRequest
from support.device_harness import Harness


def test_device_starts_in_boot_and_boot_ends_on_the_first_tick_by_default():
    harness = Harness()
    assert harness.device.state is State.BOOT
    harness.tick()
    assert harness.device.state is State.HOMING


def test_boot_lasts_as_long_as_configured():
    harness = Harness(boot_duration_ms=100)
    harness.advance(99)
    assert harness.device.state is State.BOOT
    harness.advance(1)
    assert harness.device.state is State.HOMING


def test_boot_then_homing_then_disarmed_at_the_right_times():
    harness = Harness()
    harness.tick()
    assert harness.device.state is State.HOMING
    harness.advance(2999)
    assert harness.device.state is State.HOMING
    harness.advance(1)
    assert harness.device.state is State.DISARMED
    changes = [
        (e.device_ms, e.details["old"], e.details["new"])
        for e in harness.sink.named("state_changed")
    ]
    assert changes == [
        (0, State.BOOT, State.HOMING),
        (3000, State.HOMING, State.DISARMED),
    ]


def test_homing_time_follows_the_setting():
    harness = Harness(homing_duration_ms=500)
    harness.advance(499)
    assert harness.device.state is State.HOMING
    harness.advance(1)
    assert harness.device.state is State.DISARMED


def test_never_armed_on_boot_and_homed_only_after_homing():
    harness = Harness()
    for _ in range(300):
        harness.advance(10)
        assert harness.device.armed is False
        assert harness.device.homed is (harness.device.state is State.DISARMED)
    assert harness.device.homed is True


def test_exactly_one_drift_event_when_homing_completes():
    harness = Harness()
    harness.advance(3000)
    harness.advance(5000)
    drifts = harness.sink.named("homing_drift")
    assert len(drifts) == 1
    assert drifts[0].device_ms == 3000
    assert drifts[0].details["drift_cdeg"] == harness.device.simulated_drift_cdeg


def test_homing_leaves_the_head_at_the_homed_zero():
    harness = Harness.homed()
    assert (harness.device.pan, harness.device.tilt) == (0, 0)


def reply_block(harness: Harness) -> StatusBlock:
    reply = harness.reply(StatusRequest())
    assert reply.result is Result.OK
    assert reply.request_type == MsgType.STATUS_REQUEST
    block = StatusBlock.decode_payload(reply.data)
    assert isinstance(block, StatusBlock)
    return block


def test_status_block_fields_are_correct_in_boot():
    harness = Harness(boot_duration_ms=500)
    block = reply_block(harness)
    assert block == StatusBlock(State.BOOT, 0, 0, False, False, False, True, False, 0, 0)


def test_status_block_fields_are_correct_in_homing():
    harness = Harness()
    harness.advance(1000)
    block = reply_block(harness)
    assert block.state is State.HOMING
    assert (block.armed, block.homed, block.effector_on, block.fault_mask) == (
        False,
        False,
        False,
        0,
    )


def test_status_block_fields_are_correct_in_disarmed():
    harness = Harness.homed()
    block = reply_block(harness)
    assert block == StatusBlock(State.DISARMED, 0, 0, False, False, True, True, False, 0, 0)


def test_status_request_is_answered_in_every_state_reached_so_far():
    for wait_ms, expected in [(0, State.BOOT), (1, State.HOMING), (4000, State.DISARMED)]:
        harness = Harness(boot_duration_ms=1)
        if wait_ms:
            harness.advance(wait_ms)
        assert reply_block(harness).state is expected


def test_status_event_is_sent_on_change_and_then_once_a_second():
    harness = Harness()
    harness.advance(3000)
    states = [o.message.status.state for o in harness.events_of(EventType.STATUS)]
    # HOMING when it starts, repeated each second while nothing changes, DISARMED on change.
    assert states == [State.HOMING, State.HOMING, State.HOMING, State.DISARMED]


def test_status_event_repeats_every_second_when_nothing_changes():
    harness = Harness.homed()
    harness.log.clear()
    harness.advance(3500)
    assert len(harness.events_of(EventType.STATUS)) == 3


def test_status_event_decodes_to_the_same_block_as_the_reply():
    harness = Harness.homed()
    event = harness.events_of(EventType.STATUS)[-1].message
    assert isinstance(event, StatusEvent)
    assert event.status == reply_block(harness)


def test_no_unnamed_state_value_is_ever_reported():
    harness = Harness(boot_duration_ms=50)
    valid = {int(state) for state in State}
    for _ in range(500):
        harness.advance(10)
        assert int(harness.device.state) in valid
    for out in harness.events_of(EventType.STATUS):
        assert int(out.message.status.state) in valid
