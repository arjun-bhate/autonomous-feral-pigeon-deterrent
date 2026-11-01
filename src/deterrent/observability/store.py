"""The observability database: one writer thread, many cheap callers.

Everything the laptop records (log lines, traces with their spans, metric events) goes
to one SQLite file, ``data/observability.sqlite3``. SQLite is a small database that
lives in a single file.

The rule that shapes this module: **code that is doing something important must never
wait for the disk**. So callers do not write to the database. They call ``submit()``,
which only puts the record on an in-memory queue and returns at once. A separate
*writer thread* (a second line of execution running at the same time as the rest of
the program) takes records off the queue and writes them in batches.

Reading uses its own connection (``open_read_connection``) that is opened read-only,
so a reader can never change the data. The database runs in WAL mode (write-ahead
logging), which lets readers and the writer work at the same time without blocking
each other.

All times are UTC epoch microseconds (whole numbers of millionths of a second since
1 January 1970).
"""

import json
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_REPO_ROOT = Path(__file__).resolve().parents[3]
_WRITER_WAKE_S = 0.2


def default_db_path() -> Path:
    """``data/observability.sqlite3`` at the repository root (never the device folder)."""
    return _REPO_ROOT / "data" / "observability.sqlite3"


def wall_clock_us() -> int:
    """The real time of day as UTC epoch microseconds."""
    return time.time_ns() // 1000


# ---- the records callers submit -------------------------------------------------------------


@dataclass(frozen=True)
class LogRecord:
    id: int
    ts_us: int
    component: str
    severity: str
    message: str
    trace_id: str | None = None
    span_id: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SpanRecord:
    span_id: str
    trace_id: str
    position: int
    name: str
    started_us: int
    duration_us: int | None
    status: str
    simulated: bool
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TraceRecord:
    """A finished trace and all its spans, written together in one transaction."""

    trace_id: str
    started_us: int
    ended_us: int | None
    outcome: str
    deny_reason: str | None
    simulated: bool
    spans: tuple[SpanRecord, ...] = ()


@dataclass(frozen=True)
class MetricRecord:
    ts_us: int
    name: str
    kind: str  # counter | gauge | sample
    value: float
    unit: str
    labels: dict[str, Any] = field(default_factory=dict)


Record = LogRecord | TraceRecord | MetricRecord


def _json_or_none(values: dict[str, Any]) -> str | None:
    return json.dumps(values, default=str, sort_keys=True) if values else None


class ObservabilityStore:
    """Writes records to SQLite from a background thread. See the module notes."""

    def __init__(self, path: Path | str | None = None, *, start: bool = True) -> None:
        self.path = Path(path) if path is not None else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._queue: queue.SimpleQueue[Record] = queue.SimpleQueue()
        self._pump_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._writer: sqlite3.Connection | None = None
        self._id_lock = threading.Lock()
        self._create_schema()
        self._last_log_id = self._highest_log_id()
        if start:
            self._thread = threading.Thread(
                target=self._run, name="observability-writer", daemon=True
            )
            self._thread.start()

    # ---- setup ------------------------------------------------------------------------------

    def _connect_writer(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=0.25, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _create_schema(self) -> None:
        conn = self._connect_writer()
        try:
            conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            conn.commit()
        finally:
            conn.close()

    def _highest_log_id(self) -> int:
        conn = self.open_read_connection()
        try:
            return conn.execute("SELECT COALESCE(MAX(id), 0) FROM logs").fetchone()[0]
        finally:
            conn.close()

    def open_read_connection(self) -> sqlite3.Connection:
        """A new connection that can only read. The caller closes it."""
        conn = sqlite3.connect(self.path, timeout=0.25, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn

    # ---- the caller side -----------------------------------------------------------------------

    def next_log_id(self) -> int:
        """A new log id. Ids only ever go up, so they give a stable order and paging."""
        with self._id_lock:
            self._last_log_id += 1
            return self._last_log_id

    def submit(self, record: Record) -> None:
        """Queue a record for writing. Never waits for the disk and never raises."""
        self._queue.put(record)
        self._wake.set()

    # ---- the writer side -----------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(_WRITER_WAKE_S)
            self._wake.clear()
            self.pump()

    def _drain_queue(self) -> list[Record]:
        batch: list[Record] = []
        while True:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                return batch

    def pump(self) -> None:
        """One writer cycle: take everything queued and write it. Thread-safe."""
        with self._pump_lock:
            batch = self._drain_queue()
            if batch:
                self._write(batch)

    def flush(self) -> None:
        """Write everything queued right now, on the calling thread."""
        self.pump()

    def _write(self, batch: list[Record]) -> None:
        if self._writer is None:
            self._writer = self._connect_writer()
        conn = self._writer
        with conn:  # one transaction: all of the batch is saved, or none of it
            for record in batch:
                self._write_one(conn, record)

    @staticmethod
    def _write_one(conn: sqlite3.Connection, record: Record) -> None:
        if isinstance(record, LogRecord):
            conn.execute(
                "INSERT INTO logs (id, ts_us, component, severity, message, trace_id, span_id,"
                " fields_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.id,
                    record.ts_us,
                    record.component,
                    record.severity,
                    record.message,
                    record.trace_id,
                    record.span_id,
                    _json_or_none(record.fields),
                ),
            )
        elif isinstance(record, MetricRecord):
            conn.execute(
                "INSERT INTO metric_events (ts_us, name, kind, labels_json, value, unit)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    record.ts_us,
                    record.name,
                    record.kind,
                    _json_or_none(record.labels),
                    record.value,
                    record.unit,
                ),
            )
        else:
            conn.execute(
                "INSERT INTO traces (trace_id, started_us, ended_us, outcome, deny_reason,"
                " simulated) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    record.trace_id,
                    record.started_us,
                    record.ended_us,
                    record.outcome,
                    record.deny_reason,
                    int(record.simulated),
                ),
            )
            for span in record.spans:
                conn.execute(
                    "INSERT INTO spans (span_id, trace_id, position, name, started_us,"
                    " duration_us, status, simulated, attrs_json)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        span.span_id,
                        span.trace_id,
                        span.position,
                        span.name,
                        span.started_us,
                        span.duration_us,
                        span.status,
                        int(span.simulated),
                        _json_or_none(span.attrs),
                    ),
                )

    # ---- shutting down ------------------------------------------------------------------------

    def close(self) -> None:
        """Stop the writer thread, save what is queued, close the database."""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        try:
            self.pump()
        finally:
            if self._writer is not None:
                self._writer.close()
                self._writer = None
