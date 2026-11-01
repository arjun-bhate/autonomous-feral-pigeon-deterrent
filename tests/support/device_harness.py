"""Test helper: a simulated device on a fake clock, with its output decoded.

Not a test file. Tests build a ``Harness``, send protocol messages and read back the
decoded replies and events, so no test has to build or parse bytes by hand.
"""

from dataclasses import dataclass

from deterrent.clock import FakeClock
from deterrent.config import Settings, load_settings
from deterrent.protocol.codes import MsgType, State
from deterrent.protocol.framing import Frame, build_frame, parse_frame
from deterrent.protocol.messages import (
    DeviceMessage,
    Heartbeat,
    HostMessage,
    Reply,
    decode_device_message,
    host_frame,
)
from deterrent.simulator.device import Device
from deterrent.simulator.events import RecordingSink
from deterrent.simulator.zone_storage import MockZoneStorage

TICK_MS = 10


@dataclass(frozen=True)
class Out:
    """One frame the device sent, decoded."""

    seq: int
    type: int
    message: DeviceMessage
    raw: bytes


class Harness:
    def __init__(self, *, boot_duration_ms: int = 0, **overrides: int) -> None:
        base = load_settings().model_dump()
        base.update(overrides)
        self.settings = Settings(**base)
        self.clock = FakeClock()
        self.sink = RecordingSink()
        self.storage = MockZoneStorage()
        self.device = Device(
            self.clock,
            self.settings,
            self.sink,
            self.storage,
            boot_duration_ms=boot_duration_ms,
            allow_test_injection=True,
        )
        self.seq = 0
        self.log: list[Out] = []

    # ---- building a device that is past boot and homing --------------------------------

    @classmethod
    def homed(cls, *, key_on: bool = False, **overrides: int) -> "Harness":
        """A device that has finished BOOT and HOMING and is DISARMED."""
        harness = cls(**overrides)
        harness.advance(harness.settings.homing_duration_ms)
        assert harness.device.state is State.DISARMED
        if key_on:
            harness.device.set_key_level(True)
        return harness

    # ---- sending ---------------------------------------------------------------------------

    def _collect(self, frames: list[bytes]) -> list[Out]:
        outs = []
        for wire in frames:
            frame = parse_frame(wire[:-1])
            assert isinstance(frame, Frame), frame
            message = decode_device_message(frame.type, frame.payload)
            assert not isinstance(message, int), message
            outs.append(Out(frame.seq, frame.type, message, wire))
        self.log.extend(outs)
        return outs

    def next_seq(self) -> int:
        self.seq = (self.seq + 1) & 0xFFFF
        return self.seq

    def send_wire(self, wire: bytes) -> list[Out]:
        return self._collect(self.device.handle_bytes(wire))

    def send(self, message: HostMessage, seq: int | None = None) -> list[Out]:
        return self.send_wire(host_frame(message, self.next_seq() if seq is None else seq))

    def send_raw(self, msg_type: int, payload: bytes = b"", seq: int | None = None) -> list[Out]:
        return self.send_wire(
            build_frame(msg_type, self.next_seq() if seq is None else seq, payload)
        )

    def reply(self, message: HostMessage, seq: int | None = None):
        """Send a message and return the one reply to it (not events)."""
        outs = self.send(message, seq)
        replies = [o for o in outs if not 0xC0 <= o.type <= 0xC2]
        assert len(replies) == 1, outs
        return replies[0].message

    def heartbeat(self, person_clear: bool = True) -> Reply:
        return self.reply(Heartbeat(person_clear, 0))

    # ---- time -------------------------------------------------------------------------------

    def tick(self) -> list[Out]:
        return self._collect(self.device.tick())

    def advance(self, ms: int) -> list[Out]:
        """Move the clock forward in steps of at most TICK_MS, ticking the device each step."""
        outs: list[Out] = []
        remaining = ms
        while remaining > 0:
            step = min(TICK_MS, remaining)
            self.clock.advance(step)
            outs += self.tick()
            remaining -= step
        if ms == 0:
            outs += self.tick()
        return outs

    # ---- looking at the result -----------------------------------------------------------

    def events_of(self, event_type: int) -> list[Out]:
        return [o for o in self.log if o.type == event_type]

    def snapshot(self) -> tuple:
        """Everything that must not change when a message is rejected."""
        device = self.device
        return (
            device.state,
            device.pan,
            device.tilt,
            device.armed,
            device.homed,
            device.effector_on,
            device.fault_mask,
            tuple(self.storage.read_all()),
        )

    def request_type_of(self, msg_type: MsgType) -> int:
        return int(msg_type) | 0x80
