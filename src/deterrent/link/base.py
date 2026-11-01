"""The link: how bytes travel between the laptop and the device.

A link only moves bytes. It knows nothing about messages, state or decisions, and it
holds no reference to the device. Today the link is a pair of in-memory queues to the
simulator; later it will be a USB serial port or a network socket to the real ESP32.
Everything above it (the client) stays the same when the link changes.
"""

from typing import Protocol


class DeviceLink(Protocol):
    """One end of a byte pipe."""

    async def send(self, data: bytes) -> None:
        """Send bytes towards the other end."""
        ...

    async def receive(self) -> bytes | None:
        """Wait for the next chunk of bytes. Returns None once the link has closed."""
        ...
