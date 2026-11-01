"""The simulated ESP32: one object that behaves like the real device's firmware.

Bytes go in through ``handle_bytes``; replies and unsolicited events come back as
ready-to-send wire frames. Time passes only when ``tick`` is called (or a message
arrives), and the device reads time only from the ``Clock`` it was given, so a test
with a ``FakeClock`` controls every timing rule exactly.

This device is the only place a fire request can be granted, and only by the single
decision function in ``arbitration.py``. Nothing here can switch the effector on any
other way.

Intake (this section) follows the receiver steps of protocol.md section 2:
1. framing, 2. length, 3. CRC (failures: no reply, counted), 4. sequence rule,
5. known type, 6. exact payload length, 7. field ranges (failures: a normal
REJECTED reply), 8. the command is allowed in the current state (each handler).
"""

from collections.abc import Callable

from deterrent.clock import Clock
from deterrent.config import Settings
from deterrent.protocol.codes import MsgType, RejectCode, State
from deterrent.protocol.framing import Frame, FrameError, FrameReader
from deterrent.protocol.messages import (
    DeviceMessage,
    GenericReject,
    HostMessage,
    Reply,
    decode_host_message,
    device_frame,
)
from deterrent.simulator.events import DeviceEventSink
from deterrent.simulator.zone_storage import MockZoneStorage, ZoneStorage

Handler = Callable[[HostMessage], Reply]

_KNOWN_TYPES = frozenset(int(t) for t in MsgType)


class Device:
    def __init__(
        self,
        clock: Clock,
        settings: Settings,
        sink: DeviceEventSink | None = None,
        storage: ZoneStorage | None = None,
        *,
        boot_duration_ms: int = 0,
        simulated_drift_cdeg: int = 3,
        allow_test_injection: bool = False,
    ) -> None:
        self._clock = clock
        self.settings = settings
        self._sink = sink if sink is not None else DeviceEventSink()
        self.storage: ZoneStorage = storage if storage is not None else MockZoneStorage()
        self._boot_duration_ms = boot_duration_ms
        self._simulated_drift_cdeg = simulated_drift_cdeg
        self._allow_test_injection = allow_test_injection

        self.state = State.BOOT
        self.pan = 0
        self.tilt = 0
        self.armed = False
        self.homed = False
        self.effector_on = False
        self.fault_mask = 0

        self._reader = FrameReader()
        self._out: list[bytes] = []
        self._event_seq = 0
        self.reject_counts: dict[RejectCode, int] = {}
        # Command handlers by message class. A valid message with no handler is refused
        # (WRONG_STATE) rather than accepted. Tests may replace entries.
        self.handlers: dict[type, Handler] = {}

    # ---- the outside world --------------------------------------------------------------

    def now_ms(self) -> int:
        return self._clock.now_ms()

    def handle_bytes(self, data: bytes) -> list[bytes]:
        """Feed received bytes; returns the wire frames the device sends in response."""
        for item in self._reader.feed(data):
            self._service()
            if isinstance(item, FrameError):
                self._on_bad_frame(item)
            else:
                self._on_frame(item)
            self._service()
        return self._drain()

    def tick(self) -> list[bytes]:
        """Let time-based rules run. Must be called at least every 10 ms."""
        self._service()
        return self._drain()

    def _drain(self) -> list[bytes]:
        out, self._out = self._out, []
        return out

    def _send_event(self, message: DeviceMessage) -> None:
        self._out.append(device_frame(message, self._event_seq))
        self._event_seq = (self._event_seq + 1) & 0xFFFF

    # ---- time-based work (grows task by task) ----------------------------------------------

    def _service(self) -> None:
        pass

    # ---- intake: steps 1 to 3 -----------------------------------------------------------------

    def _on_bad_frame(self, error: FrameError) -> None:
        """Framing, length or CRC failure: no reply (the sequence number cannot be trusted)."""
        cause = RejectCode[error.name]
        self.reject_counts[cause] = self.reject_counts.get(cause, 0) + 1
        self._sink.message_rejected(self.now_ms(), cause, None)

    # ---- intake: steps 4 to 8 ------------------------------------------------------------------

    def _on_frame(self, frame: Frame) -> None:
        reply = self._process(frame)
        self._out.append(device_frame(reply, frame.seq))

    def _process(self, frame: Frame) -> Reply | GenericReject:
        decoded = decode_host_message(frame.type, frame.payload)
        if isinstance(decoded, RejectCode):
            self._sink.message_rejected(self.now_ms(), decoded, frame.type)
            if decoded is RejectCode.UNKNOWN_TYPE:
                return GenericReject(frame.type)
            return Reply.rejected(frame.type, decoded)
        handler = self.handlers.get(type(decoded))
        if handler is None:
            self._sink.message_rejected(self.now_ms(), RejectCode.WRONG_STATE, frame.type)
            return Reply.rejected(frame.type, RejectCode.WRONG_STATE)
        return handler(decoded)
