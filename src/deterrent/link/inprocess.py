"""A link between the laptop code and the simulator, inside one program.

It is two queues. Whatever the ``host`` end sends comes out of the ``device`` end, and
the other way round. The host end goes to the client; the device end goes to the
simulator runner. Nothing else is connected: bytes in, bytes out.
"""

import asyncio


class _End:
    """One end of the pipe: sends into the peer's queue, receives from its own."""

    def __init__(self, inbox: "asyncio.Queue[bytes | None]", peer_inbox) -> None:
        self._inbox = inbox
        self._peer_inbox = peer_inbox

    async def send(self, data: bytes) -> None:
        await self._peer_inbox.put(bytes(data))

    async def receive(self) -> bytes | None:
        return await self._inbox.get()


class InProcessLink:
    def __init__(self) -> None:
        to_host: asyncio.Queue[bytes | None] = asyncio.Queue()
        to_device: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.host = _End(inbox=to_host, peer_inbox=to_device)
        self.device = _End(inbox=to_device, peer_inbox=to_host)
        self._queues = (to_host, to_device)

    async def close(self) -> None:
        """Tell both ends the link is over (their next ``receive`` returns None)."""
        for queue in self._queues:
            await queue.put(None)
