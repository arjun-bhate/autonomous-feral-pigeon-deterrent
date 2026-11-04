"""The live channel to the browser: one WebSocket, server to client only.

A *WebSocket* is a connection a browser keeps open so the server can push messages the
moment something happens, instead of the browser asking again and again.

Every message is ``{"type": ..., "data": ...}``:

===================  ==========================================================
``snapshot``         sent once, right after connecting: device, system, recent logs
``device``           the device status, on every change and at least once a second
``device_offline``   the simulator stopped (the page must show "unavailable")
``log``              one new log entry
``storage_warning``  saving to disk started failing (``{"message": ...}``) or recovered (null)
``tick``             every second, so the browser can tell the server is alive
===================  ==========================================================

``health``           the laptop readings (CPU, GPU, memory, temperatures), every second

Rules this module keeps:

* Everything it sends comes from **memory** (the platform's live status, the logger's
  recent list), never from a database query, so a slow or failing disk cannot delay it.
* Each browser has its own small queue and sender. A browser that stops reading cannot
  hold up the others, and one that falls far behind is dropped.
* The browser sends nothing that matters: anything it sends is read and ignored.
"""

import asyncio
import contextlib
from typing import Any, Protocol

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect

from deterrent.api.routes_read import health_payload, log_item, system_status
from deterrent.composition import Platform
from deterrent.observability.store import LogRecord, StorageWarning

router = APIRouter()

TICK_SECONDS = 1.0
DEVICE_REFRESH_MS = 1000
SNAPSHOT_LOGS = 50
QUEUE_LIMIT = 100


class Socket(Protocol):
    async def accept(self) -> None: ...

    async def send_json(self, data: Any) -> None: ...

    async def receive_text(self) -> str: ...


class _Client:
    """One connected browser: a queue of messages and a task that sends them in order."""

    def __init__(self, socket: Socket) -> None:
        self.socket = socket
        self.queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self.task = asyncio.create_task(self._send_loop())

    async def _send_loop(self) -> None:
        while True:
            await self.socket.send_json(await self.queue.get())

    def close(self) -> None:
        self.task.cancel()


class LiveHub:
    def __init__(self, platform: Platform) -> None:
        self._platform = platform
        self._clients: set[_Client] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tick_task: asyncio.Task | None = None
        self._active = False
        self._last_device_ms = 0

    @property
    def client_count(self) -> int:
        return len(self._clients)

    # ---- lifecycle ----

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._active = True
        self._platform.add_device_listener(self._on_device)
        self._platform.add_storage_listener(self._on_storage_warning)
        self._platform.logger.subscribe(self._on_log)
        self._tick_task = asyncio.create_task(self._tick_loop())

    async def stop(self) -> None:
        self._active = False
        if self._tick_task is not None:
            self._tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._tick_task
            self._tick_task = None
        for client in list(self._clients):
            client.close()
        self._clients.clear()

    # ---- serving one browser ----

    async def serve(self, socket: Socket) -> None:
        """Accept a browser, send the snapshot, then keep it until it disconnects."""
        await socket.accept()
        client = _Client(socket)
        client.queue.put_nowait({"type": "snapshot", "data": self._snapshot()})
        self._clients.add(client)
        try:
            while True:
                await socket.receive_text()  # read and ignore: the channel is one-way
        except WebSocketDisconnect:
            pass
        finally:
            self._drop(client)

    def _snapshot(self) -> dict[str, Any]:
        platform = self._platform
        return {
            "health": health_payload(platform),
            "device": platform.device_status(),
            "system": system_status(platform, platform.settings.dev_tools),
            "recent_logs": [log_item(r) for r in platform.logger.recent(SNAPSHOT_LOGS)],
        }

    def _drop(self, client: _Client) -> None:
        client.close()
        self._clients.discard(client)

    # ---- sending ----

    def _broadcast(self, message: dict) -> None:
        for client in list(self._clients):
            try:
                client.queue.put_nowait(message)
            except asyncio.QueueFull:
                self._drop(client)  # too far behind

    def _post(self, message: dict) -> None:
        """Broadcast from any thread: the work is handed to the event loop."""
        if not self._active or self._loop is None:
            return
        with contextlib.suppress(RuntimeError):  # the loop is already closed
            self._loop.call_soon_threadsafe(self._broadcast, message)

    def _on_device(self, status: dict | None) -> None:
        if status is None:
            self._post({"type": "device_offline", "data": {}})
        else:
            self._last_device_ms = self._platform.clock.now_ms()
            self._post({"type": "device", "data": status})

    def _on_log(self, record: LogRecord) -> None:
        self._post({"type": "log", "data": log_item(record)})

    def _on_storage_warning(self, warning: StorageWarning | None) -> None:
        data = None if warning is None else {"message": warning.message}
        self._post({"type": "storage_warning", "data": data})

    async def _tick_loop(self) -> None:
        while True:
            await self._platform.sleep(TICK_SECONDS)
            self._broadcast({"type": "tick", "data": {"server_us": self._platform.wall_clock_us()}})
            self._broadcast({"type": "health", "data": health_payload(self._platform)})
            self._refresh_device()

    def _refresh_device(self) -> None:
        """Make sure a running device is reported at least once a second."""
        status = self._platform.device_status()
        if status.get("running"):
            idle = (self._platform.clock.now_ms() - self._last_device_ms) % (2**32)
            if idle >= DEVICE_REFRESH_MS:
                self._last_device_ms = self._platform.clock.now_ms()
                self._broadcast({"type": "device", "data": status})


@router.websocket("/ws/live")
async def live(websocket: WebSocket) -> None:
    await websocket.app.state.hub.serve(websocket)
