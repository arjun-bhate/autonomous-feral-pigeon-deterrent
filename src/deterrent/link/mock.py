"""A link that damages traffic on command, to test that the protocol copes.

``FaultInjectingLink`` wraps another link (the host end of the in-process pair). By
default it passes everything through. A test can ask for the next message in either
direction to be:

* **corrupted**: one bit changed, so the receiver's checksum fails;
* **duplicated**: delivered twice;
* **dropped**: never delivered;
* **delayed**: delivered later, so messages sent after it can overtake it;
* **reordered**: held back until the message after it has been delivered.

``to_device`` controls traffic sent by the host; ``to_host`` controls what the host
receives. A request such as ``drop_next(2)`` applies to the next two messages.
"""

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable

from deterrent.link.base import DeviceLink

_FLIP = 0x10


def _corrupt(data: bytes) -> bytes:
    """Change one bit of a byte in the middle of the data, never creating a 0x00 delimiter."""
    if len(data) < 3:
        return data
    index = len(data) // 2
    damaged = bytearray(data)
    damaged[index] ^= _FLIP
    if damaged[index] == 0:
        damaged[index] ^= _FLIP | 0x20
    return bytes(damaged)


class _Lane:
    """The faults planned for one direction of traffic."""

    def __init__(
        self, deliver: Callable[[bytes], Awaitable[None]], sleep: Callable[[float], Awaitable[None]]
    ) -> None:
        self._deliver = deliver
        self._sleep = sleep
        self._plan: deque[tuple[str, int]] = deque()
        self._held: bytes | None = None
        self._delayed: set[asyncio.Task] = set()

    # ---- planning ---------------------------------------------------------------------------

    def corrupt_next(self, count: int = 1) -> None:
        self._plan.extend([("corrupt", 0)] * count)

    def duplicate_next(self, count: int = 1) -> None:
        self._plan.extend([("duplicate", 0)] * count)

    def drop_next(self, count: int = 1) -> None:
        self._plan.extend([("drop", 0)] * count)

    def delay_next(self, ms: int) -> None:
        self._plan.append(("delay", ms))

    def reorder_next(self) -> None:
        self._plan.append(("reorder", 0))

    # ---- doing ------------------------------------------------------------------------------

    async def push(self, data: bytes) -> None:
        kind, argument = self._plan.popleft() if self._plan else ("pass", 0)
        if kind == "drop":
            return
        if kind == "reorder":
            self._held = data
            return
        if kind == "delay":
            task = asyncio.create_task(self._deliver_later(data, argument))
            self._delayed.add(task)
            task.add_done_callback(self._delayed.discard)
            return
        if kind == "corrupt":
            await self._deliver(_corrupt(data))
        else:
            await self._deliver(data)
            if kind == "duplicate":
                await self._deliver(data)
        if self._held is not None:
            held, self._held = self._held, None
            await self._deliver(held)

    async def _deliver_later(self, data: bytes, ms: int) -> None:
        await self._sleep(ms / 1000)
        await self._deliver(data)

    def cancel(self) -> None:
        for task in list(self._delayed):
            task.cancel()


class FaultInjectingLink:
    def __init__(
        self,
        inner: DeviceLink,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._inner = inner
        self._incoming: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.to_device = _Lane(inner.send, sleep)
        self.to_host = _Lane(self._incoming.put, sleep)
        self._pump: asyncio.Task | None = None

    async def send(self, data: bytes) -> None:
        await self.to_device.push(data)

    async def receive(self) -> bytes | None:
        if self._pump is None:
            self._pump = asyncio.create_task(self._pump_incoming())
        return await self._incoming.get()

    async def _pump_incoming(self) -> None:
        while True:
            data = await self._inner.receive()
            if data is None:
                await self._incoming.put(None)
                return
            await self.to_host.push(data)

    async def close(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
        self.to_device.cancel()
        self.to_host.cancel()
