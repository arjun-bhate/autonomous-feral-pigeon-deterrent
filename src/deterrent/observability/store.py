"""The observability database: one writer thread, many cheap callers.

Everything the laptop records (log lines, traces with their spans, metric events) goes
to one SQLite file, ``data/observability.sqlite3``. SQLite is a small database that
lives in a single file.

The rule that shapes this module: **code that is doing something important must never
wait for the disk**. So callers do not write to the database. They call ``submit()``,
which only puts the record on an in-memory queue and returns at once. A separate
*writer thread* (a second line of execution running at the same time as the rest of
the program) takes records off the queue and writes them in batches.

If the disk fails (full or locked) the store keeps running: the newest records wait
in a memory *ring* (a fixed-size buffer that drops its oldest entry when full), a
storage warning is raised, and the write is retried every 2 seconds. When the disk
works again everything kept is saved and the warning clears.

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
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_REPO_ROOT = Path(__file__).resolve().parents[3]
_WRITER_WAKE_S = 0.2
RING_SIZE = 5000
RETRY_INTERVAL_MS = 2000
_FAULT_MODES = {
    "ok": None,
    "full": "database or disk is full",
    "locked": "database is locked",
}
_MAX_NOTICES = 50


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


@dataclass(frozen=True)
class StorageWarning:
    """Shown on the dashboard while saving to disk is failing."""

    message: str
    since_us: int


def _json_or_none(values: dict[str, Any]) -> str | None:
    return json.dumps(values, default=str, sort_keys=True) if values else None


class ObservabilityStore:
    """Writes records to SQLite from a background thread. See the module notes."""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        start: bool = True,
        clock_us: Callable[[], int] = wall_clock_us,
        ring_size: int = RING_SIZE,
        retry_interval_ms: int = RETRY_INTERVAL_MS,
    ) -> None:
        self.path = Path(path) if path is not None else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock_us = clock_us
        self._retry_interval_us = retry_interval_ms * 1000
        self._ring: deque[Record] = deque(maxlen=ring_size)
        self._notices: list[LogRecord] = []  # the store's own fault and recovery lines
        self._next_retry_us = 0
        self._fault_mode = "ok"
        self._warning: StorageWarning | None = None
        self._warning_listeners: list[Callable[[StorageWarning | None], None]] = []
        self.on_log: Callable[[LogRecord], None] | None = None
        self.failed_attempts = 0
        self.last_internal_error: Exception | None = None
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
            try:
                self.pump()
            except Exception as error:  # a bug must not kill the writer thread
                self.last_internal_error = error

    def _drain_queue(self) -> list[Record]:
        batch: list[Record] = []
        while True:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                return batch

    def pump(self) -> None:
        """One writer cycle: save what is queued (and what is being held). Thread-safe.

        While the disk is failing, new records join the ring, and a write is attempted
        again only when the retry time has come.
        """
        with self._pump_lock:
            new = self._drain_queue()
            now = self._clock_us()
            failing = self._warning is not None
            if failing:
                self._ring.extend(new)
                if now < self._next_retry_us:
                    return
                batch = [*self._ring, *self._notices]
            else:
                batch = new
            if not batch:
                return
            try:
                self._write(batch)
            except sqlite3.Error as error:
                self._on_write_failed([] if failing else new, error, now)
            else:
                self._on_write_ok(len(batch), now)

    def flush(self) -> None:
        """Write everything queued right now, on the calling thread."""
        self.pump()

    # ---- failure handling -------------------------------------------------------------------

    def _on_write_failed(self, held: list[Record], error: sqlite3.Error, now: int) -> None:
        self.failed_attempts += 1
        self._ring.extend(held)
        self._next_retry_us = now + self._retry_interval_us
        if self._warning is None:
            self._raise_warning(error, now)

    def _raise_warning(self, error: sqlite3.Error, now: int) -> None:
        self._warning = StorageWarning(
            f"Saving logs and metrics to disk is failing ({error}). The newest records are "
            "kept in memory and saving is retried every 2 seconds.",
            now,
        )
        self._notice("ERROR", f"storage fault: {error}", now)
        for listener in list(self._warning_listeners):
            listener(self._warning)

    def _on_write_ok(self, saved: int, now: int) -> None:
        was_failing = self._warning is not None
        self._ring.clear()
        self._notices.clear()
        if was_failing:
            self._warning = None
            self._notice("INFO", f"storage recovered; {saved} kept records saved", now, queued=True)
            for listener in list(self._warning_listeners):
                listener(None)

    def _notice(self, severity: str, message: str, now: int, *, queued: bool = False) -> None:
        """A log line written by the store itself about its own health."""
        record = LogRecord(self.next_log_id(), now, "store", severity, message)
        if queued:
            self._queue.put(record)
        else:
            self._notices.append(record)
            del self._notices[:-_MAX_NOTICES]
        if self.on_log is not None:
            self.on_log(record)

    @property
    def storage_warning(self) -> StorageWarning | None:
        return self._warning

    def add_warning_listener(self, listener: Callable[[StorageWarning | None], None]) -> None:
        """Call ``listener`` with the warning when it is raised and with None when it clears."""
        self._warning_listeners.append(listener)

    def held_records(self) -> tuple[Record, ...]:
        """The records waiting in memory because the disk is failing (oldest first)."""
        with self._pump_lock:
            return tuple(self._ring)

    def set_fault_mode(self, mode: str) -> None:
        """Test hook: make every write fail as if the disk were ``full`` or ``locked``."""
        if mode not in _FAULT_MODES:
            raise ValueError(f"unknown storage mode {mode!r}; use ok, full or locked")
        self._fault_mode = mode

    def _write(self, batch: list[Record]) -> None:
        message = _FAULT_MODES[self._fault_mode]
        if message is not None:
            raise sqlite3.OperationalError(message)
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
