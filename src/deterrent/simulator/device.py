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
from deterrent.config import PAN_SOFT_LIMIT_CDEG, TILT_SOFT_LIMIT_CDEG, Settings
from deterrent.protocol.codes import DenyReason, Fault, MsgType, RejectCode, Result, State
from deterrent.protocol.framing import Frame, FrameError, FrameReader
from deterrent.protocol.messages import (
    Arm,
    DeviceMessage,
    Disarm,
    GenericReject,
    Heartbeat,
    Home,
    HostMessage,
    Jog,
    Move,
    RejectNotice,
    Reply,
    StatusBlock,
    StatusEvent,
    StatusRequest,
    decode_host_message,
    device_frame,
    pack_angles,
    pack_heartbeat_data,
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
_STATUS_INTERVAL_MS = 1000


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
        self.simulated_drift_cdeg = simulated_drift_cdeg
        self._allow_test_injection = allow_test_injection

        self.state = State.BOOT
        self.pan = 0
        self.tilt = 0
        self.armed = False
        self.homed = False
        self.effector_on = False
        self.fault_mask = 0
        self.key_on = False
        self._heartbeat_seen = False
        self._last_heartbeat_ms = 0

        self._boot_started_ms = clock.now_ms()
        self._homing_started_ms = 0
        self._last_status: StatusBlock | None = None
        self._last_status_ms = 0

        self._reader = FrameReader()
        self._out: list[bytes] = []
        self._event_seq = 0
        self.reject_counts: dict[RejectCode, int] = {}
        self._notice_times: deque[int] = deque()
        self._last_seq: int | None = None
        self._reply_cache: OrderedDict[int, bytes] = OrderedDict()
        # Command handlers by message class. A valid message with no handler is refused
        # (WRONG_STATE) rather than accepted. Tests may replace entries.
        self.handlers: dict[type, Handler] = {
            StatusRequest: self._on_status_request,
            Home: self._on_home,
            Move: self._on_move,
            Jog: self._on_jog,
            Arm: self._on_arm,
            Disarm: self._on_disarm,
            Heartbeat: self._on_heartbeat,
        }

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

    # ---- state and status -----------------------------------------------------------------

    def _set_state(self, new: State) -> None:
        if new is not self.state:
            old, self.state = self.state, new
            self._sink.state_changed(self.now_ms(), old, new)

    def status_block(self) -> StatusBlock:
        return StatusBlock(
            state=self.state,
            pan=self.pan,
            tilt=self.tilt,
            key_on=self.key_on,
            armed=self.armed,
            homed=self.homed,
            zone_store_valid=self._zone_store_valid(),
            effector_on=self.effector_on,
            fault_mask=self.fault_mask,
            zone_count=self._zone_count(),
        )

    def _zone_store_valid(self) -> bool:
        return True  # replaced by the derived check when stored zones arrive (T023)

    def _zone_count(self) -> int:
        return 0  # replaced when stored zones arrive (T023)

    def _on_status_request(self, message: StatusRequest) -> Reply:
        return Reply.ok(MsgType.STATUS_REQUEST, self.status_block().encode_payload())

    def _send_status_if_due(self) -> None:
        """Send a STATUS event when anything in the block changed, and at least once a second."""
        block = self.status_block()
        now = self.now_ms()
        if (
            block != self._last_status
            or elapsed_ms(now, self._last_status_ms) >= _STATUS_INTERVAL_MS
        ):
            self._last_status = block
            self._last_status_ms = now
            self._send_event(StatusEvent(block))

    # ---- time-based work ----------------------------------------------------------------------

    def _service(self) -> None:
        """Everything that happens because time passed. Runs on every tick and message."""
        now = self.now_ms()
        self._check_watchdog(now)
        self._check_recovery()
        if self.state is State.BOOT:
            if elapsed_ms(now, self._boot_started_ms) >= self._boot_duration_ms:
                self._homing_started_ms = now
                self._set_state(State.HOMING)
        if self.state is State.HOMING:
            if elapsed_ms(now, self._homing_started_ms) >= self.settings.homing_duration_ms:
                self._finish_homing()
        self._send_status_if_due()

    def _check_watchdog(self, now: int) -> None:
        """No accepted heartbeat for more than watchdog_ms means the host is gone."""
        if (
            self._heartbeat_seen
            and not self.fault_mask & Fault.HEARTBEAT_LOST
            and elapsed_ms(now, self._last_heartbeat_ms) > self.settings.watchdog_ms
        ):
            self._enter_safe_error(Fault.HEARTBEAT_LOST, DenyReason.HEARTBEAT_LOST)

    def _enter_safe_error(self, fault: Fault, cause: DenyReason) -> None:
        """Latch a fault and go to SAFE_ERROR: armed is cleared and the lapse is counted."""
        self.fault_mask |= int(fault)
        self._sink.interlock_event(self.now_ms(), cause)
        if self.state is State.SAFE_ERROR:
            return  # already there; only the extra fault is recorded
        self.armed = False
        if self.state is State.HOMING:
            self.homed = False  # homing is abandoned, not resumed
        self._set_state(State.SAFE_ERROR)

    def _check_recovery(self) -> None:
        """Leave SAFE_ERROR once every latched fault's clearing condition is true.

        HEARTBEAT_LOST is cleared by an accepted heartbeat (in its handler); KEY_OFF
        clears when the key is on. Any other bit never clears. The device arrives in
        DISARMED and is never re-armed by itself.
        """
        if self.state is not State.SAFE_ERROR:
            return
        if self.fault_mask & Fault.KEY_OFF and self.key_on:
            self.fault_mask &= ~int(Fault.KEY_OFF)
        if self.fault_mask == 0:
            self._set_state(State.DISARMED)

    def _finish_homing(self) -> None:
        self.homed = True
        self.pan = 0
        self.tilt = 0
        self._sink.homing_drift(self.now_ms(), self.simulated_drift_cdeg)
        self._set_state(State.DISARMED)

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
            reply = Reply.rejected(frame.type, RejectCode.WRONG_STATE)
        else:
            reply = handler(decoded)
        if reply.result is Result.REJECTED:
            self._sink.message_rejected(self.now_ms(), RejectCode(reply.code), frame.type)
        return reply

    # ---- step 8: commands ------------------------------------------------------------------------

    def _on_home(self, message: Home) -> Reply:
        """Start homing. Only allowed when DISARMED; it ends in DISARMED with homed true."""
        if self.state is State.HOMING:
            return Reply.rejected(MsgType.HOME, RejectCode.BUSY_HOMING)
        if self.state is not State.DISARMED:
            return Reply.rejected(MsgType.HOME, RejectCode.WRONG_STATE)
        self.homed = False
        self._homing_started_ms = self.now_ms()
        self._set_state(State.HOMING)
        return Reply.ok(MsgType.HOME)

    def _motion_refusal(self, msg_type: MsgType) -> Reply | None:
        """The reason MOVE or JOG cannot be done in the current state, or None if it can."""
        if self.state is State.HOMING:
            return Reply.rejected(msg_type, RejectCode.BUSY_HOMING)
        if self.state is State.BOOT:
            return Reply.rejected(msg_type, RejectCode.NOT_HOMED)
        if self.state is State.SAFE_ERROR:
            return Reply.rejected(msg_type, RejectCode.WRONG_STATE)
        if not self.homed:
            return Reply.rejected(msg_type, RejectCode.NOT_HOMED)
        return None

    def _go_to(self, msg_type: MsgType, pan: int, tilt: int) -> Reply:
        """Move the head to an absolute position, or refuse the whole move (never clamp)."""
        refusal = self._motion_refusal(msg_type)
        if refusal is not None:
            return refusal
        if not (0 <= pan <= PAN_SOFT_LIMIT_CDEG and 0 <= tilt <= TILT_SOFT_LIMIT_CDEG):
            return Reply.rejected(msg_type, RejectCode.OUT_OF_SOFT_LIMIT)
        if (pan, tilt) != (self.pan, self.tilt):
            self.pan, self.tilt = pan, tilt
            self._sink.angles_changed(self.now_ms(), pan, tilt)
        return Reply.ok(msg_type, pack_angles(self.pan, self.tilt))

    def _on_move(self, message: Move) -> Reply:
        return self._go_to(MsgType.MOVE, message.pan, message.tilt)

    def _on_jog(self, message: Jog) -> Reply:
        return self._go_to(MsgType.JOG, self.pan + message.dpan, self.tilt + message.dtilt)

    def _on_arm(self, message: Arm) -> Reply:
        """Arm the device. Checks run in a fixed order and the first failure is returned."""
        if self.armed:
            return Reply.ok(MsgType.ARM)  # already armed: nothing to do
        if self.state is State.HOMING:
            return Reply.rejected(MsgType.ARM, RejectCode.BUSY_HOMING)
        if not self.homed:
            return Reply.rejected(MsgType.ARM, RejectCode.NOT_HOMED)
        if not self.key_on:
            return Reply.rejected(MsgType.ARM, RejectCode.KEY_OFF)
        if self.fault_mask != 0:
            return Reply.rejected(MsgType.ARM, RejectCode.FAULT_ACTIVE)
        if self.state is not State.DISARMED:
            return Reply.rejected(MsgType.ARM, RejectCode.WRONG_STATE)
        self.armed = True
        self._set_state(State.SCANNING)
        return Reply.ok(MsgType.ARM)

    def _on_disarm(self, message: Disarm) -> Reply:
        """Always accepted. In BOOT, HOMING and SAFE_ERROR it changes nothing else."""
        if self.state not in (State.BOOT, State.HOMING, State.SAFE_ERROR):
            self.armed = False
            self._set_state(State.DISARMED)
        return Reply.ok(MsgType.DISARM)

    def _on_heartbeat(self, message: Heartbeat) -> Reply:
        """The host is alive. Allowed in every state except BOOT."""
        if self.state is State.BOOT:
            return Reply.rejected(MsgType.HEARTBEAT, RejectCode.WRONG_STATE)
        now = self.now_ms()
        self._heartbeat_seen = True
        self._last_heartbeat_ms = now
        if self.fault_mask & Fault.HEARTBEAT_LOST:
            self.fault_mask &= ~int(Fault.HEARTBEAT_LOST)
            self._check_recovery()
        return Reply.ok(MsgType.HEARTBEAT, pack_heartbeat_data(now, self.state))

    # ---- inputs the hardware would provide -----------------------------------------------------

    def set_key_level(self, on: bool) -> None:
        """The arm key changed. (The key-off safety rule is added with the SAFE_ERROR rules.)"""
        self.key_on = on
