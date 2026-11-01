"""Tests for the ZoneStorage interface and MockZoneStorage (T013)."""

import pytest

from deterrent.simulator.zone_storage import (
    MockZoneStorage,
    StorageError,
    StorageMode,
    StoredZone,
    ZoneStorage,
)


def zone(zone_id=1, crc=0xAABBCCDD, count=3, points=bytes(range(12))):
    return StoredZone(zone_id, crc, count, points)


def test_mock_is_a_zone_storage():
    assert isinstance(MockZoneStorage(), ZoneStorage)


def test_new_store_is_empty():
    assert MockZoneStorage().read_all() == []


def test_write_then_read():
    storage = MockZoneStorage()
    storage.write_zone(zone(1))
    storage.write_zone(zone(2, crc=5))
    assert storage.read_all() == [zone(1), zone(2, crc=5)]


def test_writing_an_existing_id_replaces_it():
    storage = MockZoneStorage()
    storage.write_zone(zone(1, crc=1))
    storage.write_zone(zone(1, crc=2))
    assert storage.read_all() == [zone(1, crc=2)]


@pytest.mark.parametrize("mode", [StorageMode.FULL, StorageMode.LOCKED])
def test_write_in_full_or_locked_mode_raises_and_keeps_old_data(mode):
    storage = MockZoneStorage()
    storage.write_zone(zone(1))
    storage.mode = mode
    with pytest.raises(StorageError):
        storage.write_zone(zone(2))
    with pytest.raises(StorageError):
        storage.write_zone(zone(1, crc=99))
    storage.mode = StorageMode.OK
    assert storage.read_all() == [zone(1)]


def test_unreadable_mode_raises_on_read_and_write():
    storage = MockZoneStorage()
    storage.write_zone(zone(1))
    storage.mode = StorageMode.UNREADABLE
    with pytest.raises(StorageError):
        storage.read_all()
    with pytest.raises(StorageError):
        storage.write_zone(zone(2))
    storage.mode = StorageMode.OK
    assert storage.read_all() == [zone(1)]


def test_locked_and_unreadable_also_refuse_delete_and_clear():
    for mode in (StorageMode.LOCKED, StorageMode.UNREADABLE):
        storage = MockZoneStorage()
        storage.write_zone(zone(1))
        storage.mode = mode
        with pytest.raises(StorageError):
            storage.delete_zone(1)
        with pytest.raises(StorageError):
            storage.clear()
        storage.mode = StorageMode.OK
        assert storage.read_all() == [zone(1)]


def test_full_mode_still_allows_delete_and_clear_to_free_space():
    storage = MockZoneStorage()
    storage.write_zone(zone(1))
    storage.write_zone(zone(2))
    storage.mode = StorageMode.FULL
    storage.delete_zone(1)
    assert [z.zone_id for z in storage.read_all()] == [2]
    storage.clear()
    assert storage.read_all() == []


def test_corrupting_bytes_does_not_change_the_stored_crc():
    storage = MockZoneStorage()
    storage.write_zone(zone(1))
    storage.corrupt_zone(1)
    (stored,) = storage.read_all()
    assert stored.crc32 == 0xAABBCCDD
    assert stored.points != bytes(range(12))
    assert len(stored.points) == 12


def test_corrupting_an_unknown_zone_is_an_error():
    with pytest.raises(KeyError):
        MockZoneStorage().corrupt_zone(9)


def test_delete_removes_one_zone_and_ignores_unknown_ids():
    storage = MockZoneStorage()
    storage.write_zone(zone(1))
    storage.write_zone(zone(2))
    storage.delete_zone(1)
    storage.delete_zone(77)
    assert [z.zone_id for z in storage.read_all()] == [2]


def test_clear_empties_the_store():
    storage = MockZoneStorage()
    storage.write_zone(zone(1))
    storage.clear()
    assert storage.read_all() == []


def test_read_returns_a_copy_so_callers_cannot_change_the_store():
    storage = MockZoneStorage()
    storage.write_zone(zone(1))
    storage.read_all().clear()
    assert len(storage.read_all()) == 1
