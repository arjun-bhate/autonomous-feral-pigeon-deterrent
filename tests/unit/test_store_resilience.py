"""Tests for the observability store, part 2 (T045): a full or locked disk is survived."""

import time
from pathlib import Path

import pytest

from deterrent.clock import FakeClock
from deterrent.observability.store import (
    LogRecord,
    MetricRecord,
    ObservabilityStore,
    StorageWarning,
)


def make_store(
    tmp_path: Path, *, start: bool = False, **kw
) -> tuple[ObservabilityStore, FakeClock]:
    clock = FakeClock(1_000_000)
    store = ObservabilityStore(
        tmp_path / "obs.sqlite3", start=start, clock_us=lambda: clock.now_ms() * 1000, **kw
    )
    return store, clock


def log(store: ObservabilityStore, clock: FakeClock, message: str = "m") -> LogRecord:
    return LogRecord(store.next_log_id(), clock.now_ms() * 1000, "api", "INFO", message)


def count(store: ObservabilityStore, table: str = "logs") -> int:
    conn = store.open_read_connection()
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


@pytest.mark.parametrize("mode", ["locked", "full"])
def test_submit_never_blocks_or_raises_while_storage_fails(tmp_path, mode):
    store, clock = make_store(tmp_path)
    store.set_fault_mode(mode)
    started = time.perf_counter()
    for _ in range(50):
        store.submit(log(store, clock))
        store.pump()
    assert time.perf_counter() - started < 1.0
    store.close()


@pytest.mark.parametrize("mode", ["locked", "full"])
def test_the_warning_appears_within_10_seconds_of_clock_time(tmp_path, mode):
    store, clock = make_store(tmp_path)
    assert store.storage_warning is None
    store.set_fault_mode(mode)
    submitted_at = clock.now_ms() * 1000
    store.submit(log(store, clock))
    for _ in range(10):  # one pump per second of fake time, up to ten seconds
        store.pump()
        if store.storage_warning is not None:
            break
        clock.advance(1000)
    warning = store.storage_warning
    assert isinstance(warning, StorageWarning)
    assert warning.since_us - submitted_at <= 10_000_000
    assert warning.message
    store.close()


def test_the_ring_keeps_only_the_newest_5000_records(tmp_path):
    store, clock = make_store(tmp_path)
    store.set_fault_mode("locked")
    ids = []
    for index in range(6000):
        record = log(store, clock, f"entry {index}")
        ids.append(record.id)
        store.submit(record)
    store.pump()
    held = [r for r in store.held_records() if isinstance(r, LogRecord) and r.component == "api"]
    assert len(held) == 5000
    assert [r.id for r in held] == ids[-5000:]
    store.set_fault_mode("ok")
    clock.advance(2000)
    store.pump()
    assert count(store) >= 5000
    conn = store.open_read_connection()
    saved = [r[0] for r in conn.execute("SELECT id FROM logs WHERE component = 'api' ORDER BY id")]
    conn.close()
    assert saved == ids[-5000:]
    store.close()


def test_the_ring_size_can_be_set_smaller(tmp_path):
    store, clock = make_store(tmp_path, ring_size=3)
    store.set_fault_mode("full")
    for index in range(10):
        store.submit(log(store, clock, f"e{index}"))
    store.pump()
    assert len(store.held_records()) == 3
    store.close()


def test_retries_happen_every_2_seconds_not_more_often(tmp_path):
    store, clock = make_store(tmp_path)
    store.set_fault_mode("locked")
    store.submit(log(store, clock))
    store.pump()
    attempts = store.failed_attempts
    assert attempts == 1
    clock.advance(1999)
    store.pump()
    assert store.failed_attempts == attempts  # too early
    clock.advance(1)
    store.pump()
    assert store.failed_attempts == attempts + 1  # exactly 2 s after the last try
    clock.advance(2000)
    store.pump()
    assert store.failed_attempts == attempts + 2


def test_after_recovery_the_kept_records_are_saved_and_the_warning_clears(tmp_path):
    store, clock = make_store(tmp_path)
    store.set_fault_mode("locked")
    for index in range(5):
        store.submit(log(store, clock, f"kept {index}"))
        store.submit(
            MetricRecord(clock.now_ms() * 1000, "fire_requests_granted", "counter", 1.0, "count")
        )
    store.pump()
    assert store.storage_warning is not None
    assert count(store) == 0
    store.set_fault_mode("ok")
    clock.advance(1000)
    store.pump()
    assert store.storage_warning is not None  # not yet: the retry is not due
    clock.advance(1000)
    store.pump()
    assert store.storage_warning is None
    conn = store.open_read_connection()
    messages = [
        r[0] for r in conn.execute("SELECT message FROM logs WHERE component = 'api' ORDER BY id")
    ]
    conn.close()
    assert messages == [f"kept {i}" for i in range(5)]
    assert count(store, "metric_events") == 5
    assert store.held_records() == ()
    store.close()


def test_the_fault_and_the_recovery_are_logged(tmp_path):
    store, clock = make_store(tmp_path)
    seen: list[LogRecord] = []
    store.on_log = seen.append
    store.set_fault_mode("full")
    store.submit(log(store, clock))
    store.pump()
    assert [r.severity for r in seen] == ["ERROR"]
    assert seen[0].component == "store"
    store.set_fault_mode("ok")
    clock.advance(2000)
    store.pump()
    store.pump()
    assert [r.severity for r in seen] == ["ERROR", "INFO"]
    conn = store.open_read_connection()
    rows = conn.execute(
        "SELECT severity FROM logs WHERE component = 'store' ORDER BY id"
    ).fetchall()
    conn.close()
    assert [r[0] for r in rows] == ["ERROR", "INFO"]
    store.close()


def test_warning_listeners_hear_about_the_raise_and_the_clear(tmp_path):
    store, clock = make_store(tmp_path)
    heard: list[StorageWarning | None] = []
    store.add_warning_listener(heard.append)
    store.set_fault_mode("locked")
    store.submit(log(store, clock))
    store.pump()
    store.set_fault_mode("ok")
    clock.advance(2000)
    store.pump()
    assert len(heard) == 2
    assert isinstance(heard[0], StorageWarning) and heard[1] is None
    store.close()


def test_a_thousand_submits_in_a_failing_store_cause_no_crash(tmp_path):
    store, clock = make_store(tmp_path)
    store.set_fault_mode("locked")
    for index in range(1000):
        store.submit(log(store, clock, f"x{index}"))
        if index % 100 == 0:
            clock.advance(500)
            store.pump()
    store.pump()
    assert len(store.held_records()) >= 1
    store.close()


def test_data_already_saved_can_still_be_read_while_writes_fail(tmp_path):
    store, clock = make_store(tmp_path)
    store.submit(log(store, clock, "before the fault"))
    store.pump()
    store.set_fault_mode("locked")
    store.submit(log(store, clock, "during the fault"))
    store.pump()
    conn = store.open_read_connection()
    messages = [r[0] for r in conn.execute("SELECT message FROM logs")]
    conn.close()
    assert messages == ["before the fault"]
    held = [r.message for r in store.held_records() if r.component == "api"]
    assert held == ["during the fault"]  # live data is still reachable from memory
    store.close()


def test_a_bad_mode_name_is_refused(tmp_path):
    store, _ = make_store(tmp_path)
    with pytest.raises(ValueError):
        store.set_fault_mode("broken")
    store.close()


def test_the_real_writer_thread_raises_the_warning_and_recovers(tmp_path):
    store = ObservabilityStore(tmp_path / "obs.sqlite3", retry_interval_ms=50)
    store.set_fault_mode("locked")
    store.submit(LogRecord(store.next_log_id(), 1, "api", "INFO", "threaded"))
    deadline = time.monotonic() + 10
    while store.storage_warning is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert store.storage_warning is not None
    store.set_fault_mode("ok")
    while store.storage_warning is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert store.storage_warning is None
    store.close()
    assert count(store) >= 1
