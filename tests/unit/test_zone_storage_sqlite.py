"""Tests for SqliteZoneStorage (T014). All files live in a temporary folder."""

import sqlite3
import time
from pathlib import Path

import pytest

from deterrent.simulator import zone_storage
from deterrent.simulator.zone_storage import (
    DEFAULT_ZONE_DB_PATH,
    SqliteZoneStorage,
    StorageError,
    StoredZone,
    ZoneStorage,
)


def zone(zone_id=1, crc=0x11223344, count=3, points=bytes(range(12))):
    return StoredZone(zone_id, crc, count, points)


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "device" / "zone_store.sqlite3"


def test_is_a_zone_storage_and_creates_its_folder(path):
    storage = SqliteZoneStorage(path)
    assert isinstance(storage, ZoneStorage)
    assert path.parent.is_dir()


def test_default_path_is_data_device_zone_store():
    assert DEFAULT_ZONE_DB_PATH.parts[-3:] == ("data", "device", "zone_store.sqlite3")


def test_table_layout(path):
    SqliteZoneStorage(path)
    with sqlite3.connect(path) as conn:
        columns = conn.execute("PRAGMA table_info(zone)").fetchall()
    assert [(c[1], c[2], c[5]) for c in columns] == [
        ("zone_id", "INTEGER", 1),
        ("crc32", "INTEGER", 0),
        ("point_count", "INTEGER", 0),
        ("points", "BLOB", 0),
    ]


def test_write_then_read(path):
    storage = SqliteZoneStorage(path)
    storage.write_zone(zone(2))
    storage.write_zone(zone(1, crc=0xFFFFFFFF))
    assert storage.read_all() == [zone(1, crc=0xFFFFFFFF), zone(2)]


def test_data_survives_closing_and_reopening(path):
    SqliteZoneStorage(path).write_zone(zone(4))
    assert SqliteZoneStorage(path).read_all() == [zone(4)]


def test_replace_delete_and_clear(path):
    storage = SqliteZoneStorage(path)
    storage.write_zone(zone(1, crc=1))
    storage.write_zone(zone(1, crc=2))
    storage.write_zone(zone(2))
    assert [z.crc32 for z in storage.read_all()] == [2, 0x11223344]
    storage.delete_zone(1)
    storage.delete_zone(99)
    assert [z.zone_id for z in storage.read_all()] == [2]
    storage.clear()
    assert storage.read_all() == []


def test_a_failed_write_in_the_middle_of_a_transaction_leaves_old_zones_intact(path):
    storage = SqliteZoneStorage(path)
    storage.write_zone(zone(1, crc=7))
    storage.write_zone(zone(2))
    # point_count None breaks the NOT NULL rule after the old row was already deleted
    # inside the same transaction; the rollback must bring the old row back.
    broken = StoredZone(1, 8, None, b"x")
    with pytest.raises(StorageError):
        storage.write_zone(broken)
    assert storage.read_all() == [zone(1, crc=7), zone(2)]


def test_a_locked_database_raises_instead_of_hanging(path):
    storage = SqliteZoneStorage(path, timeout_s=0.05)
    storage.write_zone(zone(1))
    blocker = sqlite3.connect(path, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        with pytest.raises(StorageError):
            storage.write_zone(zone(2))
        with pytest.raises(StorageError):
            storage.read_all()
        assert time.monotonic() - started < 2.0
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert storage.read_all() == [zone(1)]


def test_an_unusable_file_raises_storage_error(tmp_path):
    bad = tmp_path / "not_a_database.sqlite3"
    bad.write_bytes(b"this is not a sqlite file" * 100)
    with pytest.raises(StorageError):
        SqliteZoneStorage(bad)


def test_only_the_zone_store_file_is_ever_created(path):
    storage = SqliteZoneStorage(path)
    storage.write_zone(zone(1))
    storage.read_all()
    storage.clear()
    created = {p.name for p in path.parent.rglob("*") if p.is_file()}
    assert all(name.startswith("zone_store.sqlite3") for name in created), created
    assert not list(path.parent.parent.glob("*.sqlite3"))


def test_module_never_names_the_logging_database():
    text = Path(zone_storage.__file__).read_text(encoding="utf-8")
    assert "observability.sqlite3" not in text
