"""The laptop's side of the conversation with the device.

``DeviceClient`` sends protocol messages over a link and hands back exactly what the
device replied. It does four jobs:

* **Sequence numbers.** Every message carries a 16-bit counter that goes up by one and
  wraps from 65535 to 0, so the device can spot duplicates and old messages.
* **Request and reply matching.** A reply carries the number of the request it answers.
  The client waits for the matching one, for at most ``command_ttl_ms``; if nothing
  arrives it raises ``CommandTimeout``. It never retries by itself, because repeating a
  fire request is not something to do automatically.
* **The heartbeat.** Every ``heartbeat_interval_ms`` it tells the device "the laptop is
  alive, and the camera sees no person" (or "a person is present"). If the heartbeats
  stop, the device's watchdog puts it in a safe state. The "person clear" value comes
  from a provider function (default: always clear) that a future detector will supply.
* **Device time.** A fire request carries ``issued_at_ms`` on the *device's* clock. The
  laptop and the device clocks differ, so the client learns the difference from the
  device time in each heartbeat reply (the offset) and applies it.

**The client cannot decide anything.** It has no code path that grants or approves a
fire request, and it never invents a reply: what ``request`` returns is a message
decoded from bytes the device sent. Whether to fire is decided by the device alone.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from deterrent.clock import U32_MODULUS, Clock, elapsed_ms
from deterrent.config import Settings
from deterrent.link.base import DeviceLink
from deterrent.protocol.codes import EventType, MsgType, RejectCode, Result
from deterrent.protocol.framing import Frame, FrameError, FrameReader
from deterrent.protocol.messages import (
    DeviceMessage,
    FireRequest,
    GenericReject,
    Heartbeat,
    HostMessage,
    Reply,
    decode_device_message,
    host_frame,
    unpack_heartbeat_data,
)

_SEQ_MODULUS = 65536


class CommandTimeout(Exception):
    """The device did not reply in time."""

    def __init__(self, seq: int, msg_type: int) -> None:
        super().__init__(f"no reply to message type {msg_type:#04x} (sequence {seq})")
        self.seq = seq
        self.msg_type = msg_type


@dataclass
class _Pending:
    msg_type: int
    sent_ms: int  # laptop clock when it was sent
    future: asyncio.Future | None  # None for a heartbeat nobody is waiting on


class DeviceClient:
    def __init__(
        self,
        link: DeviceLink,
        clock: Clock,
        settings: Settings,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        person_clear: Callable[[], bool] = lambda: True,
        on_event: Callable[[DeviceMessage], None] | None = None,
    ) -> None:
        self._link = link
        self._clock = clock
        self._settings = settings
        self._sleep = sleep
        self._person_clear = person_clear
        self._on_event = on_event
        self._seq = 0
        self._pending: dict[int, _Pending] = {}
        self._offset_ms: int | None = None
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self.unmatched_replies = 0
        self.damaged_frames = 0

    # ---- lifecycle -----------------------------------------------------------------------------

    async def start(self, *, heartbeat: bool = True) -> None:
        self._running = True
        self._tasks = [asyncio.create_task(self._receive_loop())]
        if heartbeat:
            self._tasks.append(asyncio.create_task(self._heartbeat_loop()))

    async def stop(self) -> None:
        """Stop the heartbeat and the receiving. The device's watchdog will notice."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    # ---- device time ---------------------------------------------------------------------------

    @property
    def time_offset_ms(self) -> int | None:
        """Device clock minus laptop clock (modulo 2**32), or None until a heartbeat reply."""
        return self._offset_ms

    def device_time_ms(self) -> int:
        """The current time on the device's clock, as estimated from the laptop clock."""
        offset = self._offset_ms if self._offset_ms is not None else 0
        return (self._clock.now_ms() + offset) % U32_MODULUS

    async def sync_time(self) -> None:
        """Ask for one heartbeat exchange to learn the clock offset. Failure is not an error."""
        try:
            await self.request(Heartbeat(bool(self._person_clear()), self._clock.now_ms()))
        except CommandTimeout:
            pass

    # ---- sending -------------------------------------------------------------------------------

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq = (self._seq + 1) % _SEQ_MODULUS
        return seq

    async def _transmit(self, message: HostMessage, future: asyncio.Future | None) -> int:
        seq = self._next_seq()
        self._pending[seq] = _Pending(int(message.TYPE), self._clock.now_ms(), future)
        await self._link.send(host_frame(message, seq))
        return seq

    async def request(self, message: HostMessage) -> Reply | GenericReject:
        """Send one message and return the device's reply exactly as decoded.

        Raises ``CommandTimeout`` if no matching reply arrives within ``command_ttl_ms``.
        """
        if not self._running:
            raise RuntimeError("the client is not running; call start() first")
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        seq = await self._transmit(message, future)
        timer = asyncio.ensure_future(self._sleep(self._settings.command_ttl_ms / 1000))
        try:
            done, _ = await asyncio.wait({future, timer}, return_when=asyncio.FIRST_COMPLETED)
            if future in done:
                return future.result()
            raise CommandTimeout(seq, int(message.TYPE))
        finally:
            timer.cancel()
            self._pending.pop(seq, None)

    async def fire_request(
        self,
        intended_pan: int,
        intended_tilt: int,
        duration_ms: int,
        ttl_ms: int | None = None,
    ) -> Reply | GenericReject:
        """Send a FIRE_REQUEST stamped with the device's time and return the device's reply.

        This asks; it does not decide. If the clock offset is not known yet, one
        heartbeat exchange is tried first; if that fails the laptop time is used as it is,
        which the device will deny as an expired request rather than act on.
        """
        if self._offset_ms is None:
            await self.sync_time()
        message = FireRequest(
            self.device_time_ms(),
            ttl_ms if ttl_ms is not None else self._settings.fire_ttl_ms,
            intended_pan,
            intended_tilt,
            duration_ms,
        )
        return await self.request(message)

    # ---- the heartbeat ------------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        while True:
            await self._send_heartbeat()
            await self._sleep(self._settings.heartbeat_interval_ms / 1000)

    async def _send_heartbeat(self) -> None:
        now = self._clock.now_ms()
        for seq in [
            s
            for s, p in self._pending.items()
            if p.future is None and elapsed_ms(now, p.sent_ms) > self._settings.command_ttl_ms
        ]:
            del self._pending[seq]  # a heartbeat whose reply never came
        await self._transmit(Heartbeat(bool(self._person_clear()), now), None)

    # ---- receiving ----------------------------------------------------------------------------

    async def _receive_loop(self) -> None:
        reader = FrameReader()
        while True:
            data = await self._link.receive()
            if data is None:
                return
            for item in reader.feed(data):
                if isinstance(item, FrameError):
                    self.damaged_frames += 1
                else:
                    self._on_frame(item)

    def _on_frame(self, frame: Frame) -> None:
        message = decode_device_message(frame.type, frame.payload)
        if isinstance(message, RejectCode):
            self.damaged_frames += 1
            return
        if frame.type in {int(event) for event in EventType}:
            if self._on_event is not None:
                self._on_event(message)
            return
        pending = self._pending.get(frame.seq)
        answers = isinstance(message, GenericReject) or frame.type == (
            (pending.msg_type | 0x80) if pending else -1
        )
        if pending is None or not answers:
            self.unmatched_replies += 1
            return
        del self._pending[frame.seq]
        if isinstance(message, Reply) and pending.msg_type == int(MsgType.HEARTBEAT):
            self._learn_offset(message, pending.sent_ms)
        if pending.future is not None and not pending.future.done():
            pending.future.set_result(message)

    def _learn_offset(self, reply: Reply, sent_ms: int) -> None:
        if reply.result is not Result.OK:
            return
        try:
            device_ms, _state = unpack_heartbeat_data(reply.data)
        except ValueError:
            return
        now = self._clock.now_ms()
        midpoint = (sent_ms + elapsed_ms(now, sent_ms) // 2) % U32_MODULUS
        self._offset_ms = (device_ms - midpoint) % U32_MODULUS
