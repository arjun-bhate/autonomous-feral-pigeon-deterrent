"""The simulator's side of the ``DeviceAdapter`` interface.

The vector runner (``vector_runner.py``) never touches the simulator directly. It talks
to a *device adapter*: a small object with the methods listed in ``DeviceAdapter``. This
file is the adapter for the simulated ESP32. When the real firmware exists, a different
adapter (one that talks over a serial port) is written and the same vector files run
against it unchanged.

Time here is a ``FakeClock``: the adapter moves it by hand, so a vector at 1500 ms
always sees exactly 1500 ms and the result never depends on how fast the computer is.
"""

import tomllib
from collections.abc import Mapping
from typing import Any

from deterrent.clock import FakeClock
from deterrent.config import DEFAULT_CONFIG_PATH, Settings
from deterrent.protocol.messages import StatusBlock
from deterrent.simulator.control import SimulatorControl
from deterrent.simulator.device import Device
from deterrent.simulator.events import RecordingSink
from deterrent.simulator.zone_storage import MockZoneStorage

TICK_MS = 10  # the device must be ticked at least this often (protocol.md 5.4)
BOOT_DURATION_MS = 100  # how long the simulated BOOT state lasts


def default_values() -> dict[str, Any]:
    """The defaults from ``config/default.toml``, read directly so no environment variable
    can change what a vector means."""
    return tomllib.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))


class SimulatorAdapter:
    def __init__(self) -> None:
        self.reset({})

    # ---- the DeviceAdapter methods ------------------------------------------------------

    def reset(self, config: Mapping[str, Any]) -> None:
        """Start a fresh device with these named values on top of the defaults."""
        self._clock = FakeClock()
        self._sink = RecordingSink()
        self._storage = MockZoneStorage()
        settings = Settings(**{**default_values(), **config})
        self._device = Device(
            self._clock,
            settings,
            self._sink,
            self._storage,
            boot_duration_ms=BOOT_DURATION_MS,
            allow_test_injection=True,
        )
        self._control = SimulatorControl(self._device)

    def apply_initial(self, initial: Mapping[str, Any]) -> list[bytes]:
        """Put the device in the starting situation (test-only)."""
        self._control.apply_initial(initial)
        return self._device.tick()

    def inject(self, fields: Mapping[str, Any]) -> list[bytes]:
        """Change only the named fields in the middle of a vector (test-only)."""
        self._control.inject(fields)
        return self._device.tick()

    def send(self, data: bytes) -> list[bytes]:
        """Deliver wire bytes; returns every frame the device sent in response."""
        return self._device.handle_bytes(data)

    def advance_to(self, at_ms: int) -> list[tuple[int, bytes]]:
        """Move device time to ``at_ms``, ticking at least every ``TICK_MS`` and exactly at
        ``at_ms``. Returns the frames sent meanwhile, each with the device time it was sent."""
        now = self._clock.now_ms()
        if at_ms < now:
            raise ValueError(f"time cannot go back from {now} ms to {at_ms} ms")
        frames: list[tuple[int, bytes]] = []
        while True:
            frames += [(now, frame) for frame in self._device.tick()]
            if now >= at_ms:
                return frames
            self._clock.advance(min(TICK_MS, at_ms - now))
            now = self._clock.now_ms()

    def set_key(self, on: bool) -> list[bytes]:
        """Turn the arm key. Returns the frames the device sent because of it."""
        self._control.set_key(on)
        return self._device.tick()

    def read_status(self) -> StatusBlock:
        return self._device.status_block()

    def read_zone_ids(self) -> list[int]:
        entries = self._device.zones.list_entries()
        return [] if entries is None else [entry.zone_id for entry in entries]

    def read_counters(self) -> dict[str, int]:
        """Counts of what the device reported to its event sink (simulator only).

        ``interlock.<CAUSE>`` and ``interlock.total``, ``rejected.<CODE>`` and
        ``rejected.total``, ``fire_denied.<REASON>``, ``fire_granted``, ``homing_drift``.
        """
        counts: dict[str, int] = {}

        def add(name: str) -> None:
            counts[name] = counts.get(name, 0) + 1

        for event in self._sink.events:
            if event.name == "interlock_event":
                add(f"interlock.{event.details['cause'].name}")
                add("interlock.total")
            elif event.name == "message_rejected":
                add(f"rejected.{event.details['cause'].name}")
                add("rejected.total")
            elif event.name == "fire_denied":
                add(f"fire_denied.{event.details['reason'].name}")
            elif event.name in ("fire_granted", "homing_drift"):
                add(event.name)
        return counts
