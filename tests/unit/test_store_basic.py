"""Tests for the observability store, part 1 (T044): schema, writing, reading, submit."""

import sqlite3
import time
from pathlib import Path

import pytest

from deterrent.observability.store import (
    LogRecord,
    MetricRecord,
    ObservabilityStore,
    SpanRecord,
    TraceRecord,
    default_db_path,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "observability.sqlite3"


def make_log(store: ObservabilityStore, ts_us: int, message: str = "hello", **kw) -> LogRecord:
    return LogRecord(
        id=store.next_log_id(),
        ts_us=ts_us,
        component=kw.pop("component", "api"),
        severity=kw.pop("severity", "INFO"),
        message=message,
        trace_id=kw.pop("trace_id", None),
        span_id=kw.pop("span_id", None),
        fields=kw.pop("fields", {}),
    )


def read_all(store: ObservabilityStore, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    conn = store.open_read_connection()
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def test_records_written_by_submit_are_readable(db_path):
    store = ObservabilityStore(db_path, start=False)
    store.submit(make_log(store, 1_000, "first", fields={"k": 1}))
    store.submit(
        MetricRecord(2_000, "fire_requests_denied", "counter", 1.0, "count", {"reason": "KEY_OFF"})
    )
    store.flush()
    logs = read_all(store, "SELECT * FROM logs")
    assert [(r["message"], r["ts_us"], r["component"]) for r in logs] == [("first", 1_000, "api")]
    assert logs[0]["fields_json"] == '{"k": 1}'
    metrics = read_all(store, "SELECT * FROM metric_events")
    assert len(metrics) == 1
    assert metrics[0]["labels_json"] == '{"reason": "KEY_OFF"}'
    assert metrics[0]["unit"] == "count"
    store.close()


def test_a_trace_is_stored_with_its_spans_in_one_go(db_path):
    store = ObservabilityStore(db_path, start=False)
    spans = tuple(
        SpanRecord(f"{n:08x}", "a" * 16, n, name, 100 * n, 10, "OK", n <= 4, {"x": n})
        for n, name in enumerate(
            ["capture", "encode", "network", "inference", "decision", "command", "esp32_ack"],
            start=1,
        )
    )
    store.submit(TraceRecord("a" * 16, 100, 800, "GRANTED", None, True, spans))
    store.flush()
    traces = read_all(store, "SELECT * FROM traces")
    assert len(traces) == 1
    assert (traces[0]["started_us"], traces[0]["ended_us"], traces[0]["outcome"]) == (
        100,
        800,
        "GRANTED",
    )
    assert traces[0]["simulated"] == 1
    rows = read_all(store, "SELECT * FROM spans ORDER BY position")
    assert [r["name"] for r in rows] == [s.name for s in spans]
    assert rows[0]["simulated"] == 1 and rows[6]["simulated"] == 0
    store.close()


def test_logs_are_read_back_ordered_by_time_then_id(db_path):
    store = ObservabilityStore(db_path, start=False)
    for ts in (30, 10, 20, 10):
        store.submit(make_log(store, ts, f"at {ts}"))
    store.flush()
    rows = read_all(store, "SELECT ts_us, id FROM logs ORDER BY ts_us, id")
    assert [r["ts_us"] for r in rows] == [10, 10, 20, 30]
    assert rows[0]["id"] < rows[1]["id"]
    store.close()


def test_data_survives_closing_and_reopening(db_path):
    store = ObservabilityStore(db_path)
    store.submit(make_log(store, 5, "kept"))
    store.close()
    again = ObservabilityStore(db_path, start=False)
    rows = read_all(again, "SELECT message FROM logs")
    assert [r["message"] for r in rows] == ["kept"]
    again.close()


def test_log_ids_continue_after_reopening(db_path):
    store = ObservabilityStore(db_path, start=False)
    last = make_log(store, 1)
    store.submit(last)
    store.close()
    again = ObservabilityStore(db_path, start=False)
    assert again.next_log_id() > last.id
    again.close()


def test_submit_returns_without_waiting_for_the_disk(db_path):
    store = ObservabilityStore(db_path, start=False)  # no writer thread at all
    started = time.perf_counter()
    for index in range(2000):
        store.submit(make_log(store, index))
    assert time.perf_counter() - started < 1.0
    assert read_all(store, "SELECT COUNT(*) AS n FROM logs")[0]["n"] == 0  # nothing written yet
    store.flush()
    assert read_all(store, "SELECT COUNT(*) AS n FROM logs")[0]["n"] == 2000
    store.close()


def test_the_writer_thread_saves_records_by_itself(db_path):
    store = ObservabilityStore(db_path)
    store.submit(make_log(store, 1, "threaded"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if read_all(store, "SELECT 1 FROM logs"):
            break
        time.sleep(0.01)
    assert read_all(store, "SELECT message FROM logs")[0]["message"] == "threaded"
    store.close()


def test_the_default_file_is_under_data_and_not_in_the_device_folder():
    path = default_db_path()
    assert path.name == "observability.sqlite3"
    assert path.parent.name == "data"
    assert "device" not in path.parts


def test_tables_have_exactly_the_documented_columns(db_path):
    store = ObservabilityStore(db_path, start=False)

    def columns(table: str) -> list[tuple[str, str]]:
        rows = read_all(store, f"PRAGMA table_info({table})")
        return [(r["name"], r["type"]) for r in rows]

    assert columns("logs") == [
        ("id", "INTEGER"),
        ("ts_us", "INTEGER"),
        ("component", "TEXT"),
        ("severity", "TEXT"),
        ("message", "TEXT"),
        ("trace_id", "TEXT"),
        ("span_id", "TEXT"),
        ("fields_json", "TEXT"),
    ]
    assert columns("traces") == [
        ("trace_id", "TEXT"),
        ("started_us", "INTEGER"),
        ("ended_us", "INTEGER"),
        ("outcome", "TEXT"),
        ("deny_reason", "TEXT"),
        ("simulated", "INTEGER"),
    ]
    assert columns("spans") == [
        ("span_id", "TEXT"),
        ("trace_id", "TEXT"),
        ("position", "INTEGER"),
        ("name", "TEXT"),
        ("started_us", "INTEGER"),
        ("duration_us", "INTEGER"),
        ("status", "TEXT"),
        ("simulated", "INTEGER"),
        ("attrs_json", "TEXT"),
    ]
    assert columns("metric_events") == [
        ("id", "INTEGER"),
        ("ts_us", "INTEGER"),
        ("name", "TEXT"),
        ("kind", "TEXT"),
        ("labels_json", "TEXT"),
        ("value", "REAL"),
        ("unit", "TEXT"),
    ]
    store.close()


def test_the_documented_indexes_exist(db_path):
    store = ObservabilityStore(db_path, start=False)
    index_columns = {}
    for row in read_all(store, "SELECT name, tbl_name FROM sqlite_master WHERE type = 'index'"):
        if row["name"].startswith("sqlite_"):
            continue
        cols = [c["name"] for c in read_all(store, f"PRAGMA index_info({row['name']})")]
        index_columns[(row["tbl_name"], tuple(cols))] = row["name"]
    for expected in [
        ("logs", ("ts_us",)),
        ("logs", ("component", "ts_us")),
        ("logs", ("severity", "ts_us")),
        ("logs", ("trace_id", "ts_us")),
        ("metric_events", ("name", "ts_us")),
    ]:
        assert expected in index_columns
    store.close()


def test_the_read_connection_cannot_write(db_path):
    store = ObservabilityStore(db_path, start=False)
    conn = store.open_read_connection()
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute(
            "INSERT INTO logs(id, ts_us, component, severity, message) VALUES (1,1,'a','INFO','x')"
        )
    conn.close()
    store.close()


def test_the_database_file_is_created_in_a_missing_folder(db_path):
    assert not db_path.parent.exists()
    store = ObservabilityStore(db_path, start=False)
    assert db_path.exists()
    store.close()
