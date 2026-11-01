"""Test helper: a stand-in for a browser WebSocket connection. Not a test file."""

import asyncio

from starlette.websockets import WebSocketDisconnect


class FakeSocket:
    """Stands in for a browser connection."""

    def __init__(self, *, stuck: bool = False) -> None:
        self.sent: list[dict] = []
        self.accepted = False
        self._stuck = stuck
        self._inbox: asyncio.Queue[str | None] = asyncio.Queue()

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, data: dict) -> None:
        if self._stuck:
            await asyncio.Event().wait()  # a browser that never reads
        self.sent.append(data)

    async def receive_text(self) -> str:
        item = await self._inbox.get()
        if item is None:
            raise WebSocketDisconnect()
        return item

    def says(self, text: str) -> None:
        self._inbox.put_nowait(text)

    def disconnect(self) -> None:
        self._inbox.put_nowait(None)

    def of_type(self, kind: str) -> list[dict]:
        return [m["data"] for m in self.sent if m["type"] == kind]
