-- Tables of the observability database (data-model.md sections 1.2 to 1.5).
-- All times are UTC epoch microseconds (integers). Created only if missing.

CREATE TABLE IF NOT EXISTS logs (
    id          INTEGER PRIMARY KEY,
    ts_us       INTEGER NOT NULL,
    component   TEXT    NOT NULL,
    severity    TEXT    NOT NULL,
    message     TEXT    NOT NULL,
    trace_id    TEXT,
    span_id     TEXT,
    fields_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs (ts_us);
CREATE INDEX IF NOT EXISTS idx_logs_component_ts ON logs (component, ts_us);
CREATE INDEX IF NOT EXISTS idx_logs_severity_ts ON logs (severity, ts_us);
CREATE INDEX IF NOT EXISTS idx_logs_trace_ts ON logs (trace_id, ts_us);

CREATE TABLE IF NOT EXISTS traces (
    trace_id    TEXT PRIMARY KEY,
    started_us  INTEGER NOT NULL,
    ended_us    INTEGER,
    outcome     TEXT    NOT NULL,
    deny_reason TEXT,
    simulated   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_traces_started ON traces (started_us);

CREATE TABLE IF NOT EXISTS spans (
    span_id     TEXT PRIMARY KEY,
    trace_id    TEXT    NOT NULL REFERENCES traces (trace_id),
    position    INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    started_us  INTEGER NOT NULL,
    duration_us INTEGER,
    status      TEXT    NOT NULL,
    simulated   INTEGER NOT NULL,
    attrs_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_spans_trace_position ON spans (trace_id, position);

CREATE TABLE IF NOT EXISTS metric_events (
    id          INTEGER PRIMARY KEY,
    ts_us       INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    kind        TEXT    NOT NULL,
    labels_json TEXT,
    value       REAL    NOT NULL,
    unit        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metric_events_name_ts ON metric_events (name, ts_us);
