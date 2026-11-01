"""The simulated ESP32: one object that behaves like the real device's firmware.

Bytes go in through ``handle_bytes``; replies and unsolicited events come back as
ready-to-send wire frames. Time passes only when ``tick`` is called (or a message
arrives), and the device reads time only from the ``Clock`` it was given, so a test
with a ``FakeClock`` controls every timing rule exactly.

This device is the only place a fire request can be granted, and only by the single
decision function in ``arbitration.py``. Nothing here can switch the effector on any
other way.

Intake follows the receiver steps of protocol.md section 2:
1. framing, 2. length, 3. CRC (failures: no reply, counted, a rate-limited notice),
4. sequence rule (duplicates replay the cached reply, old numbers are stale),
5. known type, 6. exact payload length, 7. field ranges (failures: a normal
REJECTED reply), 8. the command is allowed in the current state (each handler).
"""

from collections import OrderedDict, deque
from collections.abc import Callable

from deterrent.clock import Clock, elapsed_ms
from deterrent.config import Settings
from deterrent.protocol.codes import MsgType, RejectCode, State
from deterrent.protocol.framing import Frame, FrameError, FrameReader
from deterrent.protocol.messages import (
    DeviceMessage,
    GenericReject,
    HostMessage,
    RejectNotice,
    Reply,
    decode_host_message,
    device_frame,
)
from deterrent.simulator.events import DeviceEventSink
from deterrent.simulator.zone_storage import MockZoneStorage, ZoneStorage

Handler = Callable[[HostMessage], Reply]

_KNOWN_TYPES = frozenset(int(t) for t in MsgType)
_REPLY_CACHE_SIZE = 8
_NOTICES_PER_WINDOW = 5
_NOTICE_WINDOW_MS = 1000
_SEQ_MODULUS = 65536
_MAX_FORWARD_DISTANCE = 32767


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
        self._notice_times: deque[int] = deque()
        self._last_seq: int | None = None
        self._reply_cache: OrderedDict[int, bytes] = OrderedDict()
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
        now = self.now_ms()
        self._sink.message_rejected(now, cause, None)
        self._maybe_send_reject_notice(cause, now)

    def _maybe_send_reject_notice(self, cause: RejectCode, now: int) -> None:
        """Tell the host about a dropped frame, but at most 5 times in any one second."""
        while self._notice_times and elapsed_ms(now, self._notice_times[0]) >= _NOTICE_WINDOW_MS:
            self._notice_times.popleft()
        if len(self._notice_times) >= _NOTICES_PER_WINDOW:
            return
        self._notice_times.append(now)
        total = sum(self.reject_counts.values()) & 0xFFFF
        self._send_event(RejectNotice(int(cause), total))

    # ---- intake: steps 4 to 8 ------------------------------------------------------------------

    def _on_frame(self, frame: Frame) -> None:
        seq = frame.seq
        # Step 4: the sequence number rule (protocol.md section 3).
        cached = self._reply_cache.get(seq)
        if cached is not None:
            self._out.append(cached)  # a duplicate: replay the reply, do nothing again
            return
        if self._last_seq is not None:
            distance = (seq - self._last_seq) % _SEQ_MODULUS
            if not 1 <= distance <= _MAX_FORWARD_DISTANCE:
                self._on_stale(frame)
                return
        self._last_seq = seq
        wire = device_frame(self._process(frame), seq)
        # Even a rejected message uses up its number; its reply is what a duplicate replays.
        self._reply_cache[seq] = wire
        while len(self._reply_cache) > _REPLY_CACHE_SIZE:
            self._reply_cache.popitem(last=False)
        self._out.append(wire)

    def _on_stale(self, frame: Frame) -> None:
        """An older message that is not a cached duplicate: refuse it, change nothing."""
        self._sink.message_rejected(self.now_ms(), RejectCode.STALE_SEQ, frame.type)
        if frame.type in _KNOWN_TYPES:
            reply: Reply | GenericReject = Reply.rejected(frame.type, RejectCode.STALE_SEQ)
        else:
            reply = GenericReject(frame.type)  # no normal reply type exists for it
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
