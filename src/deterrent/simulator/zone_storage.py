"""Where the simulated device keeps its stored zones.

``ZoneStorage`` is the interface the device uses: read everything, write one zone
as a single all-or-nothing step, delete one, clear all. It deals in *raw bytes with
the CRC that was stored alongside them*, never in "trusted" zones, because the
device must be able to notice that stored bytes no longer match their CRC.

``MockZoneStorage`` keeps zones in memory and can be switched into failure modes
(full, locked, unreadable) or have one zone's bytes corrupted, so tests can create
every storage problem on demand. SqliteZoneStorage (bottom of the file) keeps zones in a file.
"""

import sqlite3
from abc import ABC, abstractmethod
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


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

    def stored_ids(self) -> list[int]:
        """The ids held in memory, whatever the failure mode (test use: bytes can be
        damaged while the storage is unreadable, and the test interface must see that)."""
        return list(self._zones)

    def corrupt_zone(self, zone_id: int) -> None:
        """Damage the stored bytes of one zone without touching its stored CRC."""
        zone = self._zones[zone_id]
        damaged = bytes([zone.points[0] ^ 0x01]) + zone.points[1:]
        self._zones[zone_id] = StoredZone(zone.zone_id, zone.crc32, zone.point_count, damaged)


# ---------------------------------------------------------------------------------------
# File-backed storage
# ---------------------------------------------------------------------------------------

DEFAULT_ZONE_DB_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "device" / "zone_store.sqlite3"
)


class SqliteZoneStorage(ZoneStorage):
    """Zones in a small SQLite database file, standing in for the ESP32 flash memory.

    The file belongs to the device alone and is kept in a different folder from the
    logging database on purpose. Every operation opens the file, does its work and
    closes it again, so no handle stays open. Each change is one *transaction* (a
    group of steps that all take effect, or none do): if anything fails part-way the
    database rolls back and the old zones are still there.
    """

    def __init__(self, path: Path | str = DEFAULT_ZONE_DB_PATH, timeout_s: float = 0.25) -> None:
        self._path = Path(path)
        self._timeout_s = timeout_s
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._run(self._create_table)

    @staticmethod
    def _create_table(conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS zone ("
            "zone_id INTEGER PRIMARY KEY, crc32 INTEGER, "
            "point_count INTEGER NOT NULL, points BLOB)"
        )

    def _run(self, work, write: bool = True):
        """Run ``work(conn)`` in one transaction; turn any database problem into StorageError."""
        try:
            with closing(
                sqlite3.connect(self._path, timeout=self._timeout_s, isolation_level=None)
            ) as conn:
                conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                try:
                    result = work(conn)
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")
                return result
        except sqlite3.Error as error:
            raise StorageError(str(error)) from error

    def read_all(self) -> list[StoredZone]:
        def work(conn: sqlite3.Connection) -> list[StoredZone]:
            rows = conn.execute(
                "SELECT zone_id, crc32, point_count, points FROM zone ORDER BY zone_id"
            ).fetchall()
            return [StoredZone(zid, crc, count, bytes(points)) for zid, crc, count, points in rows]

        return self._run(work, write=False)

    def write_zone(self, zone: StoredZone) -> None:
        def work(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM zone WHERE zone_id = ?", (zone.zone_id,))
            conn.execute(
                "INSERT INTO zone (zone_id, crc32, point_count, points) VALUES (?, ?, ?, ?)",
                (zone.zone_id, zone.crc32, zone.point_count, zone.points),
            )

        self._run(work)

    def delete_zone(self, zone_id: int) -> None:
        self._run(lambda conn: conn.execute("DELETE FROM zone WHERE zone_id = ?", (zone_id,)))

    def clear(self) -> None:
        self._run(lambda conn: conn.execute("DELETE FROM zone"))
