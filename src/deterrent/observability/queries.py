"""Read-only questions to ask the observability database.

Every function takes a read connection (``ObservabilityStore.open_read_connection``)
and returns plain data objects. Nothing here changes the database, and nothing here
is used by the live paths (the WebSocket and the device status read from memory), so
a slow disk cannot slow them down.

Paging, in plain words: the log list can be long, so a request returns one *page*,
the newest ``limit`` matching entries. To get the page before it, pass the smallest
id of the page you hold as ``before_id``. Entries are chosen by id (ids only ever go
up, so there are no gaps or repeats) and shown oldest to newest within the page.
"""

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from deterrent.observability.metrics import PERCENTILE_METHOD, REQUIRED_METRICS, summarize
from deterrent.observability.store import LogRecord

DEFAULT_LOG_LIMIT = 200
MAX_LOG_LIMIT = 1000
DEFAULT_TRACE_LIMIT = 50


def _parse(text: str | None) -> dict[str, Any]:
    return json.loads(text) if text else {}


# ---- logs ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LogPage:
    items: list[LogRecord]
    has_more: bool


def query_logs(
    conn: sqlite3.Connection,
    *,
    component: str | None = None,
    severity: str | None = None,
    trace_id: str | None = None,
    limit: int = DEFAULT_LOG_LIMIT,
    before_id: int | None = None,
) -> LogPage:
    """The newest matching log entries. Filters combine with AND; a limit above 1000 is 1000."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    limit = min(limit, MAX_LOG_LIMIT)
    clauses, args = [], []
    for column, value in (("component", component), ("severity", severity), ("trace_id", trace_id)):
        if value is not None:
            clauses.append(f"{column} = ?")
            args.append(value)
    if before_id is not None:
        clauses.append("id < ?")
        args.append(before_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM logs {where} ORDER BY id DESC LIMIT ?", (*args, limit + 1)
    ).fetchall()
    has_more = len(rows) > limit
    page = sorted(rows[:limit], key=lambda row: (row["ts_us"], row["id"]))
    return LogPage(
        [
            LogRecord(
                row["id"],
                row["ts_us"],
                row["component"],
                row["severity"],
                row["message"],
                row["trace_id"],
                row["span_id"],
                _parse(row["fields_json"]),
            )
            for row in page
        ],
        has_more,
    )


def list_components(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT DISTINCT component FROM logs ORDER BY component").fetchall()
    return [row[0] for row in rows]


# ---- traces -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TraceSummary:
    trace_id: str
    started_us: int
    ended_us: int | None
    total_us: int | None
    outcome: str
    deny_reason: str | None
    simulated: bool


@dataclass(frozen=True)
class SpanItem:
    span_id: str
    position: int
    name: str
    started_us: int
    duration_us: int | None
    status: str
    simulated: bool
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TraceDetail(TraceSummary):
    spans: list[SpanItem] = field(default_factory=list)


def _summary_fields(row: sqlite3.Row) -> dict[str, Any]:
    ended = row["ended_us"]
    return {
        "trace_id": row["trace_id"],
        "started_us": row["started_us"],
        "ended_us": ended,
        "total_us": None if ended is None else ended - row["started_us"],
        "outcome": row["outcome"],
        "deny_reason": row["deny_reason"],
        "simulated": bool(row["simulated"]),
    }


def list_traces(conn: sqlite3.Connection, limit: int = DEFAULT_TRACE_LIMIT) -> list[TraceSummary]:
    """The newest traces first."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    rows = conn.execute(
        "SELECT * FROM traces ORDER BY started_us DESC, trace_id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [TraceSummary(**_summary_fields(row)) for row in rows]


def get_trace(conn: sqlite3.Connection, trace_id: str) -> TraceDetail | None:
    """One trace with its spans in position order, or None if there is no such trace."""
    row = conn.execute("SELECT * FROM traces WHERE trace_id = ?", (trace_id,)).fetchone()
    if row is None:
        return None
    spans = conn.execute(
        "SELECT * FROM spans WHERE trace_id = ? ORDER BY position", (trace_id,)
    ).fetchall()
    return TraceDetail(
        **_summary_fields(row),
        spans=[
            SpanItem(
                s["span_id"],
                s["position"],
                s["name"],
                s["started_us"],
                s["duration_us"],
                s["status"],
                bool(s["simulated"]),
                _parse(s["attrs_json"]),
            )
            for s in spans
        ],
    )


# ---- metrics ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CounterTotal:
    name: str
    labels: dict[str, Any]
    total: float
    unit: str


@dataclass(frozen=True)
class GaugeLatest:
    name: str
    value: float
    unit: str
    read_at_us: int


@dataclass(frozen=True)
class DistributionItem:
    name: str
    unit: str
    count: int
    p50: float
    p95: float
    method: str = PERCENTILE_METHOD


@dataclass(frozen=True)
class MetricSummary:
    counters: list[CounterTotal]
    gauges: list[GaugeLatest]
    distributions: list[DistributionItem]


def metric_summary(conn: sqlite3.Connection) -> MetricSummary:
    counters = [
        CounterTotal(row["name"], _parse(row["labels_json"]), row["total"], row["unit"])
        for row in conn.execute(
            "SELECT name, labels_json, unit, SUM(value) AS total FROM metric_events"
            " WHERE kind = 'counter' GROUP BY name, labels_json, unit"
            " ORDER BY name, labels_json"
        )
    ]
    gauges = [
        GaugeLatest(row["name"], row["value"], row["unit"], row["ts_us"])
        for row in conn.execute(
            "SELECT m.name, m.value, m.unit, m.ts_us FROM metric_events m WHERE m.kind = 'gauge'"
            " AND m.id = (SELECT id FROM metric_events WHERE name = m.name AND kind = 'gauge'"
            " ORDER BY ts_us DESC, id DESC LIMIT 1) ORDER BY m.name"
        )
    ]
    distributions = []
    for name_row in conn.execute(
        "SELECT DISTINCT name, unit FROM metric_events WHERE kind = 'sample' ORDER BY name"
    ).fetchall():
        values = [
            row[0]
            for row in conn.execute(
                "SELECT value FROM metric_events WHERE kind = 'sample' AND name = ?",
                (name_row["name"],),
            )
        ]
        summary = summarize(values)
        if summary is not None:  # an empty distribution is omitted, never shown as zeros
            distributions.append(
                DistributionItem(
                    name_row["name"], name_row["unit"], summary.count, summary.p50, summary.p95
                )
            )
    return MetricSummary(counters, gauges, distributions)


@dataclass(frozen=True)
class Series:
    unit: str
    points: list[list[float]]


def metric_series(
    conn: sqlite3.Connection,
    name: str,
    since_us: int | None = None,
    labels: dict[str, Any] | None = None,
) -> Series | None:
    """Points ``[ts_us, value]`` for a chart, oldest first. None if the name is unknown."""
    clauses, args = ["name = ?"], [name]
    if since_us is not None:
        clauses.append("ts_us >= ?")
        args.append(since_us)
    if labels:
        clauses.append("labels_json = ?")
        args.append(json.dumps({k: str(v) for k, v in labels.items()}, sort_keys=True))
    rows = conn.execute(
        f"SELECT ts_us, value, unit FROM metric_events WHERE {' AND '.join(clauses)}"
        " ORDER BY ts_us, id",
        args,
    ).fetchall()
    if rows:
        return Series(rows[0]["unit"], [[row["ts_us"], row["value"]] for row in rows])
    known = REQUIRED_METRICS.get(name)
    if known is not None:
        return Series(known.unit, [])
    seen = conn.execute("SELECT unit FROM metric_events WHERE name = ? LIMIT 1", (name,)).fetchone()
    return Series(seen["unit"], []) if seen is not None else None
