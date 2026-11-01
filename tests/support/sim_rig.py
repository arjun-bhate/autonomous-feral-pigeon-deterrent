"""Test helper: a real simulated device, a link and a client, all on one virtual clock.

Not a test file. ``SimRig`` builds the same chain the program builds (device, runner,
in-process link, optional fault-injecting link, ``DeviceClient``) but with time under
the test's control, so a test can say "advance 5 seconds" and have the device ticks,
the heartbeats and the cooldown all happen instantly and in the right order.
"""

from deterrent.clock import U32_MODULUS, Clock, FakeClock
from deterrent.config import Settings, load_settings
from deterrent.link.client import DeviceClient
from deterrent.link.inprocess import InProcessLink
from deterrent.link.mock import FaultInjectingLink
from deterrent.simulator.control import SimulatorControl
from deterrent.simulator.device import Device
from deterrent.simulator.events import DeviceEventSink, RecordingSink
from deterrent.simulator.runner import SimulatorRunner

from .virtual_time import VirtualTime


class OffsetClock:
    """A clock that reads a fixed number of milliseconds ahead of another (the laptop's clock)."""

    def __init__(self, base: Clock, offset_ms: int) -> None:
        self._base = base
        self._offset = offset_ms

    def now_ms(self) -> int:
        return (self._base.now_ms() + self._offset) % U32_MODULUS


class RecordingLink:
    """Wraps a link and remembers every chunk sent through it, with the virtual time."""

    def __init__(self, inner, vt: VirtualTime) -> None:
        self._inner = inner
        self._vt = vt
        self.sent: list[tuple[int, bytes]] = []

    async def send(self, data: bytes) -> None:
        self.sent.append((self._vt.elapsed_ms, data))
        await self._inner.send(data)

    async def receive(self) -> bytes | None:
        return await self._inner.receive()


class SimRig:
    def __init__(
        self,
        *,
        mock: bool = False,
        laptop_offset_ms: int = 0,
        sink: DeviceEventSink | None = None,
        person_clear=lambda: True,
        on_event=None,
        boot_duration_ms: int = 0,
        **overrides: int,
    ) -> None:
        base = load_settings().model_dump()
        base.update(overrides)
        self.settings = Settings(**base)
        self.vt = VirtualTime()
        self.sink = sink if sink is not None else RecordingSink()
        self.device = Device(
            self.vt.clock,
            self.settings,
            self.sink,
            boot_duration_ms=boot_duration_ms,
            allow_test_injection=True,
        )
        self.control = SimulatorControl(self.device)
        self.pair = InProcessLink()
        host = FaultInjectingLink(self.pair.host, sleep=self.vt.sleep) if mock else self.pair.host
        self.faults = host if mock else None
        self.link = RecordingLink(host, self.vt)
        self.runner = SimulatorRunner(self.device, self.pair.device, sleep=self.vt.sleep)
        self.laptop_clock = OffsetClock(self.vt.clock, laptop_offset_ms)
        self.client = DeviceClient(
            self.link,
            self.laptop_clock,
            self.settings,
            sleep=self.vt.sleep,
            person_clear=person_clear,
            on_event=on_event,
        )

    async def start(self, *, heartbeat: bool = True, homed: bool = True) -> None:
        await self.runner.start()
        await self.client.start(heartbeat=heartbeat)
        if homed:
            await self.vt.advance(self.settings.homing_duration_ms + 100)

    async def stop(self) -> None:
        await self.client.stop()
        await self.runner.stop()

    def sent_frames(self):
        """Every frame the client sent, decoded: (elapsed_ms, Frame)."""
        from deterrent.protocol.framing import Frame, FrameReader

        reader = FrameReader()
        out = []
        for at, data in self.link.sent:
            for item in reader.feed(data):
                assert isinstance(item, Frame)
                out.append((at, item))
        return out


__all__ = ["FakeClock", "OffsetClock", "RecordingLink", "SimRig"]
