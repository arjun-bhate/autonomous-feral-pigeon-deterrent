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
from deterrent.geometry.polygon import zone_margin_cdeg
from deterrent.protocol.codes import (
    DenyReason,
    EffectorEvent,
    Fault,
    MsgType,
    RejectCode,
    Result,
    State,
)
from deterrent.protocol.framing import Frame, FrameError, FrameReader
from deterrent.protocol.messages import (
    Arm,
    DeviceMessage,
    Disarm,
    EffectorEventMessage,
    FireRequest,
    FireStop,
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
    ZoneBegin,
    ZoneClear,
    ZoneCommit,
    ZoneDelete,
    ZoneList,
    ZonePoint,
    decode_host_message,
    device_frame,
    pack_angles,
    pack_heartbeat_data,
    pack_zone_list,
)
from deterrent.simulator.arbitration import FireSnapshot, decide_fire, lapsed_interlock
from deterrent.simulator.events import DeviceEventSink
from deterrent.simulator.person import PersonCheck, PersonSnapshot, PersonTimers, evaluate_person
from deterrent.simulator.zone_storage import MockZoneStorage, ZoneStorage
from deterrent.simulator.zones import ZoneService

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
        self.zones = ZoneService(self.storage, settings)
        self._shot_started_ms = 0
        self._shot_duration_ms = 0
        self._cooldown_started_ms: int | None = None
        self._person = PersonTimers()
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
            ZoneBegin: self._on_zone_begin,
            ZonePoint: self._on_zone_point,
            ZoneCommit: self._on_zone_commit,
            ZoneDelete: self._on_zone_delete,
            ZoneList: self._on_zone_list,
            ZoneClear: self._on_zone_clear,
            FireRequest: self._on_fire_request,
            FireStop: self._on_fire_stop,
        }

    # ---- the outside world --------------------------------------------------------------

    def now_ms(self) -> int:
        return self._clock.now_ms()

    @property
    def test_injection_allowed(self) -> bool:
        """True only if this device was built for tests; see ``SimulatorControl``."""
        return self._allow_test_injection

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
        return self.zones.store_valid()

    def _zone_count(self) -> int:
        return self.zones.zone_count()

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

    def person_snapshot(self) -> PersonSnapshot:
        return self._person.snapshot()

    def person_check(self) -> PersonCheck:
        """Is a person present, and has the re-engage delay passed? (device clock only)"""
        return evaluate_person(
            self._person.snapshot(),
            self.now_ms(),
            self.settings.person_clear_holdoff_ms,
            self.settings.person_reengage_delay_ms,
        )

    # ---- time-based work ----------------------------------------------------------------------

    def _service(self) -> None:
        """Everything that happens because time passed. Runs on every tick and message."""
        now = self.now_ms()
        self._check_watchdog(now)
        self._check_recovery()
        self._check_upload_timeout(now)
        if self.state is State.BOOT:
            if elapsed_ms(now, self._boot_started_ms) >= self._boot_duration_ms:
                self._homing_started_ms = now
                self._set_state(State.HOMING)
        if self.state is State.HOMING:
            if elapsed_ms(now, self._homing_started_ms) >= self.settings.homing_duration_ms:
                self._finish_homing()
        self._check_interlocks()
        self._check_shot_end(now)
        self._check_cooldown_expiry()
        self._send_status_if_due()

    def _check_upload_timeout(self, now: int) -> None:
        """An upload that has been idle too long is thrown away and the device is DISARMED."""
        if self.zones.timed_out(now):
            self.zones.discard()
            if self.state is State.ZONE_SETUP:
                self._set_state(State.DISARMED)

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
        self._cut_effector(cause)  # a running shot stops at once; no cooldown starts
        if self.state is State.SAFE_ERROR:
            return  # already there; only the extra fault is recorded
        self.armed = False
        self.zones.discard()
        self._person.restart()
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
            self._person.restart()
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
        self.zones.discard()
        if self.state not in (State.BOOT, State.HOMING, State.SAFE_ERROR):
            if self.effector_on:  # DISARM cuts a running shot and starts the cooldown
                self._cut_and_start_cooldown(DenyReason.NOT_ARMED)
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
        # After any recovery restart, so this heartbeat itself begins the new clear period.
        self._person.on_heartbeat(now, message.person_clear, self.settings.person_clear_holdoff_ms)
        return Reply.ok(MsgType.HEARTBEAT, pack_heartbeat_data(now, self.state))

    def _on_zone_begin(self, message: ZoneBegin) -> Reply:
        """Start a zone upload. Rejected begins change nothing, not even a running upload."""
        if self.state is State.HOMING:
            return Reply.rejected(MsgType.ZONE_BEGIN, RejectCode.BUSY_HOMING)
        if not self.homed:
            return Reply.rejected(MsgType.ZONE_BEGIN, RejectCode.NOT_HOMED)
        if self.state not in (State.DISARMED, State.ZONE_SETUP):
            return Reply.rejected(MsgType.ZONE_BEGIN, RejectCode.WRONG_STATE)
        refusal = self.zones.begin(message.zone_id, message.point_count, self.now_ms())
        if refusal is not None:
            return Reply.rejected(MsgType.ZONE_BEGIN, refusal)
        self._set_state(State.ZONE_SETUP)
        return Reply.ok(MsgType.ZONE_BEGIN)

    def _on_zone_point(self, message: ZonePoint) -> Reply:
        refusal = self.zones.point(message.index, message.pan, message.tilt, self.now_ms())
        if refusal is not None:
            return Reply.rejected(MsgType.ZONE_POINT, refusal)
        return Reply.ok(MsgType.ZONE_POINT)

    def _on_zone_commit(self, message: ZoneCommit) -> Reply:
        """Check and store the uploaded zone. Every outcome ends the upload (back to DISARMED)."""
        had_upload = self.zones.upload_active
        refusal = self.zones.commit(message.crc32)
        if had_upload and self.state is State.ZONE_SETUP:
            self._set_state(State.DISARMED)
        if refusal is not None:
            return Reply.rejected(MsgType.ZONE_COMMIT, refusal)
        return Reply.ok(MsgType.ZONE_COMMIT)

    def _on_zone_delete(self, message: ZoneDelete) -> Reply:
        if self.state not in (State.DISARMED, State.ZONE_SETUP):
            return Reply.rejected(MsgType.ZONE_DELETE, RejectCode.WRONG_STATE)
        refusal = self.zones.delete(message.zone_id)
        if refusal is not None:
            return Reply.rejected(MsgType.ZONE_DELETE, refusal)
        return Reply.ok(MsgType.ZONE_DELETE)

    def _on_zone_clear(self, message: ZoneClear) -> Reply:
        if self.state not in (State.DISARMED, State.ZONE_SETUP):
            return Reply.rejected(MsgType.ZONE_CLEAR, RejectCode.WRONG_STATE)
        refusal = self.zones.clear()
        if refusal is not None:
            return Reply.rejected(MsgType.ZONE_CLEAR, refusal)
        return Reply.ok(MsgType.ZONE_CLEAR)

    def _on_zone_list(self, message: ZoneList) -> Reply:
        if self.state is State.BOOT:
            return Reply.rejected(MsgType.ZONE_LIST, RejectCode.WRONG_STATE)
        entries = self.zones.list_entries()
        if entries is None:
            return Reply.rejected(MsgType.ZONE_LIST, RejectCode.ZONE_STORAGE_ERROR)
        return Reply.ok(MsgType.ZONE_LIST, pack_zone_list(entries))

    # ---- firing --------------------------------------------------------------------------------

    def _fire_snapshot(self) -> FireSnapshot:
        """A read-only picture of everything the fire decision depends on, taken now."""
        valid = self.zones.store_valid()  # recomputed from the stored bytes, never cached
        return FireSnapshot(
            now_ms=self.now_ms(),
            state=self.state,
            homed=self.homed,
            armed=self.armed,
            key_on=self.key_on,
            fault_mask=self.fault_mask,
            last_heartbeat_ms=self._last_heartbeat_ms if self._heartbeat_seen else None,
            watchdog_ms=self.settings.watchdog_ms,
            person=self.person_check(),
            zone_store_valid=valid,
            pan=self.pan,
            tilt=self.tilt,
            elevation_min_cdeg=self.settings.elevation_min_cdeg,
            elevation_max_cdeg=self.settings.elevation_max_cdeg,
            zones=self.zones.polygons() if valid else (),
            zone_margin_cdeg=zone_margin_cdeg(self.settings.aiming_error_cdeg),
            per_shot_ms=self.settings.per_shot_ms,
            cooldown_started_ms=self._cooldown_started_ms,
            cooldown_ms=self.settings.cooldown_ms,
        )

    def _on_fire_request(self, message: FireRequest) -> Reply:
        """Grant or deny a fire request. The grant is decided only by ``decide_fire``."""
        if self.state is State.BOOT:
            # Nothing is homed yet: refuse without arbitration.
            return Reply.rejected(MsgType.FIRE_REQUEST, RejectCode.NOT_HOMED)
        if self.effector_on:
            # A shot is already running: never grant a second one or extend the first.
            return Reply.rejected(MsgType.FIRE_REQUEST, RejectCode.WRONG_STATE)
        reason = decide_fire(self._fire_snapshot(), message)
        if reason is not None:
            self._sink.fire_denied(self.now_ms(), reason)
            detail = self.fault_mask if reason is DenyReason.FAULT_ACTIVE else 0
            return Reply.denied(reason, detail)
        return self._start_shot(message.duration_ms)

    def _start_shot(self, duration_ms: int) -> Reply:
        """Only ever called with a grant from ``decide_fire``."""
        now = self.now_ms()
        self.effector_on = True
        self._shot_started_ms = now
        self._shot_duration_ms = duration_ms
        self._set_state(State.DETER)
        self._sink.fire_granted(now)
        self._sink.effector_on(now)
        self._send_event(EffectorEventMessage(EffectorEvent.ON, 0))
        return Reply.granted(now)

    def _on_fire_stop(self, message: FireStop) -> Reply:
        """Accepted in every state, with no arbitration. It can only switch the effector off.

        If a shot is running it ends: effector off, the host-stop event, the cooldown
        starts and DETER goes to COOLDOWN. The device stays armed, no fault is latched
        and it is not counted as an interlock lapse. With no shot running, nothing at all
        changes.
        """
        if self.effector_on:
            now = self.now_ms()
            self.effector_on = False
            self._sink.effector_stopped_by_host(now)
            self._send_event(EffectorEventMessage(EffectorEvent.STOPPED_BY_HOST, 0))
            self._cooldown_started_ms = now
            self._set_state(State.COOLDOWN)
        return Reply.ok(MsgType.FIRE_STOP)

    def cooldown_running(self) -> bool:
        """Is a cooldown running? Decided only by the device clock, whatever the state."""
        return (
            self._cooldown_started_ms is not None
            and elapsed_ms(self.now_ms(), self._cooldown_started_ms) < self.settings.cooldown_ms
        )

    def _cut_effector(self, cause: DenyReason) -> None:
        """Switch the effector off because a safety condition lapsed. Does nothing if it is off.

        Reports the cut with the lapsed condition's permanent code. What happens next
        (state, cooldown) depends on the cause and is decided by the caller.
        """
        if not self.effector_on:
            return
        now = self.now_ms()
        self.effector_on = False
        self._sink.effector_cut(now, int(cause))
        self._send_event(EffectorEventMessage(EffectorEvent.CUT, int(cause)))

    def _check_interlocks(self) -> None:
        """While the effector is on, re-check every safety condition that can lapse.

        Runs on every tick and after every message. The first lapsed condition cuts the
        effector at once. What follows depends on the cause: heartbeat loss and key off
        go to SAFE_ERROR (no cooldown); DISARM goes to DISARMED; every other cause ends
        the shot, starts the cooldown and leaves the device armed with no fault latched.
        """
        if not self.effector_on:
            return
        reason = lapsed_interlock(self._fire_snapshot())
        if reason is None:
            return
        if reason is DenyReason.HEARTBEAT_LOST:
            self._enter_safe_error(Fault.HEARTBEAT_LOST, reason)
        elif reason is DenyReason.KEY_OFF:
            self._enter_safe_error(Fault.KEY_OFF, reason)
        else:
            self._cut_and_start_cooldown(reason)
            if reason is DenyReason.NOT_ARMED:
                self.armed = False
                self._set_state(State.DISARMED)
            else:
                self._set_state(State.COOLDOWN)

    def _cut_and_start_cooldown(self, reason: DenyReason) -> None:
        """Count the lapse, cut the effector and start the cooldown (the shot has ended)."""
        now = self.now_ms()
        self._sink.interlock_event(now, reason)
        self._cut_effector(reason)
        self._cooldown_started_ms = now

    def _check_shot_end(self, now: int) -> None:
        """A shot ends by itself when its maximum duration is reached; the cooldown starts."""
        if self.effector_on and elapsed_ms(now, self._shot_started_ms) >= self._shot_duration_ms:
            self.effector_on = False
            self._sink.effector_ended(now)
            self._send_event(EffectorEventMessage(EffectorEvent.NORMAL_END, 0))
            self._cooldown_started_ms = now
            self._set_state(State.COOLDOWN)

    def _check_cooldown_expiry(self) -> None:
        """COOLDOWN returns to SCANNING once the cooldown is over. Other states are left alone."""
        if self.state is State.COOLDOWN and not self.cooldown_running():
            self._set_state(State.SCANNING)

    # ---- inputs the hardware would provide -----------------------------------------------------

    def set_key_level(self, on: bool) -> None:
        """The arm key changed.

        The trigger is the change from on to off, not the level. In every state except
        BOOT (the level is sampled when BOOT ends) and SAFE_ERROR (already there) it
        puts the device in SAFE_ERROR with the KEY_OFF fault.
        """
        previous = self.key_on
        self.key_on = on
        if previous and not on and self.state not in (State.BOOT, State.SAFE_ERROR):
            self._enter_safe_error(Fault.KEY_OFF, DenyReason.KEY_OFF)
