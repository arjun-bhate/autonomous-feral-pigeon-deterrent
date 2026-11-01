"""Where the simulated device keeps its stored zones.

``ZoneStorage`` is the interface the device uses: read everything, write one zone
as a single all-or-nothing step, delete one, clear all. It deals in *raw bytes with
the CRC that was stored alongside them*, never in "trusted" zones, because the
device must be able to notice that stored bytes no longer match their CRC.

``MockZoneStorage`` keeps zones in memory and can be switched into failure modes
(full, locked, unreadable) or have one zone's bytes corrupted, so tests can create
every storage problem on demand. The file-backed version is added in the next task.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class StorageError(Exception):
    """Storage could not do what was asked (full, locked, unreadable). Nothing changed."""


@dataclass(frozen=True)
class StoredZone:
    """One zone as stored: its id, the CRC-32 saved with it, and its raw point bytes.

    ``points`` is pan and tilt of each point as signed 16-bit little-endian integers.
    """

    zone_id: int
    crc32: int
    point_count: int
    points: bytes


class ZoneStorage(ABC):
    @abstractmethod
    def read_all(self) -> list[StoredZone]:
        """Every stored zone, exactly as stored. Raises StorageError if unreadable."""

    @abstractmethod
    def write_zone(self, zone: StoredZone) -> None:
        """Store one zone atomically (all of it or none of it); replaces the same id."""

    @abstractmethod
    def delete_zone(self, zone_id: int) -> None:
        """Remove one zone. An id that is not stored is ignored."""

    @abstractmethod
    def clear(self) -> None:
        """Remove every zone."""


class StorageMode(Enum):
    OK = "ok"
    FULL = "full"  # writes fail; deleting still works so space can be freed
    LOCKED = "locked"  # every change fails
    UNREADABLE = "unreadable"  # nothing can be read or changed


class MockZoneStorage(ZoneStorage):
    """In-memory storage with switchable failure modes, for tests."""

    def __init__(self) -> None:
        self.mode = StorageMode.OK
        self._zones: dict[int, StoredZone] = {}

    def _require_readable(self) -> None:
        if self.mode is StorageMode.UNREADABLE:
            raise StorageError("storage is unreadable")

    def _require_changeable(self) -> None:
        if self.mode in (StorageMode.LOCKED, StorageMode.UNREADABLE):
            raise StorageError(f"storage is {self.mode.value}")

    def read_all(self) -> list[StoredZone]:
        self._require_readable()
        return list(self._zones.values())

    def write_zone(self, zone: StoredZone) -> None:
        self._require_changeable()
        if self.mode is StorageMode.FULL:
            raise StorageError("storage is full")
        self._zones[zone.zone_id] = zone

    def delete_zone(self, zone_id: int) -> None:
        self._require_changeable()
        self._zones.pop(zone_id, None)

    def clear(self) -> None:
        self._require_changeable()
        self._zones.clear()

    def corrupt_zone(self, zone_id: int) -> None:
        """Damage the stored bytes of one zone without touching its stored CRC."""
        zone = self._zones[zone_id]
        damaged = bytes([zone.points[0] ^ 0x01]) + zone.points[1:]
        self._zones[zone_id] = StoredZone(zone.zone_id, zone.crc32, zone.point_count, damaged)
