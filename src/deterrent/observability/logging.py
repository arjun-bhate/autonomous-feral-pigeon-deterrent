"""Structured logging: every log line is a record with named parts, not free text.

A record carries a timestamp, the *component* that wrote it (``api``, ``simulator``,
``health`` ...), a severity, a message, optional ``trace_id`` and ``span_id`` that tie
the line to one engagement, and optional extra ``fields``. Records are handed to a
``submit`` function (the store's non-blocking queue) and also kept in a small
in-memory list so the dashboard can show the most recent ones without touching the
disk.

Privacy rule (constitution VIII): a log must never contain camera images or secrets.
Every record passes through a filter first:

* a **field** that holds image bytes, or whose *name* looks like a secret or an image
  (``password``, ``api_key``, ``jpeg_data`` ...), is replaced with ``[REMOVED]``;
* secrets written into the **message text** (``password=abc``) are blanked;
* a **message** that is itself image data causes the whole record to be dropped.

Each removal or drop is itself logged, naming the field but never its value.

This module is named ``logging`` inside the ``observability`` package; it does not
replace Python's built-in ``logging``, which is a different, unrelated module.
"""

import contextlib
import json
import re
from collections import deque
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from typing import Any

from deterrent.observability.store import LogRecord

SEVERITIES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
REMOVED = "[REMOVED]"
RECENT_KEPT = 200

_TRACE_ID = re.compile(r"^[0-9a-f]{16}$")
_SPAN_ID = re.compile(r"^[0-9a-f]{8}$")
_SECRET_NAME = re.compile(
    r"password|passwd|secret|token|api[_-]?key|authorization|credential|private[_-]?key",
    re.IGNORECASE,
)
_IMAGE_NAME = re.compile(r"image|pixel|jpeg|jpg|png|bitmap|footage|video|thumbnail", re.IGNORECASE)
_SECRET_IN_TEXT = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization)\b(\s*[=:]\s*)\S+"
)
_DATA_IMAGE = "data:image/"

# The ids that the current piece of work is writing about (set by ``trace_context``).
_current_ids: ContextVar[tuple[str | None, str | None]] = ContextVar(
    "log_trace_ids", default=(None, None)
)


@contextlib.contextmanager
def trace_context(trace_id: str, span_id: str | None = None) -> Iterator[None]:
    """Inside this block, log records get ``trace_id`` (and ``span_id``) automatically."""
    _check_ids(trace_id, span_id)
    token = _current_ids.set((trace_id, span_id))
    try:
        yield
    finally:
        _current_ids.reset(token)


def _check_ids(trace_id: str | None, span_id: str | None) -> None:
    if trace_id is not None and not _TRACE_ID.match(trace_id):
        raise ValueError("trace_id must be 16 lowercase hex characters")
    if span_id is not None and not _SPAN_ID.match(span_id):
        raise ValueError("span_id must be 8 lowercase hex characters")


def _is_binary(value: Any) -> bool:
    return isinstance(value, bytes | bytearray | memoryview)


def _is_image_text(value: Any) -> bool:
    return isinstance(value, str) and value.lstrip().lower().startswith(_DATA_IMAGE)


def _scrub(value: Any, name: str, removed: dict[str, str]) -> Any:
    """Return ``value`` with anything forbidden replaced; note what was removed."""
    if _SECRET_NAME.search(name):
        removed[name] = "secret"
        return REMOVED
    if _IMAGE_NAME.search(name) or _is_binary(value) or _is_image_text(value):
        removed[name] = "image data"
        return REMOVED
    if isinstance(value, dict):
        return {str(key): _scrub(item, str(key), removed) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_scrub(item, name, removed) for item in value]
    return value


class StructuredLogger:
    """Builds log records, filters them, hands them on and remembers the latest ones."""

    def __init__(
        self,
        submit: Callable[[LogRecord], None],
        *,
        next_id: Callable[[], int],
        clock_us: Callable[[], int],
    ) -> None:
        self._submit = submit
        self._next_id = next_id
        self._clock_us = clock_us
        self._recent: deque[LogRecord] = deque(maxlen=RECENT_KEPT)
        self._listeners: list[Callable[[LogRecord], None]] = []

    # ---- writing ---------------------------------------------------------------------------

    def log(
        self,
        severity: str,
        component: str,
        message: str,
        *,
        trace_id: str | None = None,
        span_id: str | None = None,
        **fields: Any,
    ) -> LogRecord | None:
        """Write one record. Returns it, or None if it had to be dropped."""
        if severity not in SEVERITIES:
            raise ValueError(f"severity must be one of {SEVERITIES}")
        if not isinstance(component, str) or not component.strip():
            raise ValueError("a log record needs a component")
        _check_ids(trace_id, span_id)
        context_trace, context_span = _current_ids.get()
        if trace_id is None:
            trace_id = context_trace
            span_id = span_id if span_id is not None else context_span
        if _is_binary(message) or _is_image_text(message):
            self._notice(f"dropped a {component} log record whose message was image data")
            return None
        if not isinstance(message, str) or not message:
            raise ValueError("a log record needs a message")
        removed: dict[str, str] = {}
        clean_fields = {name: _scrub(value, name, removed) for name, value in fields.items()}
        clean_message = _SECRET_IN_TEXT.sub(lambda m: f"{m.group(1)}{m.group(2)}{REMOVED}", message)
        if clean_message != message:
            removed["(message text)"] = "secret"
        record = LogRecord(
            self._next_id(),
            self._clock_us(),
            component.strip(),
            severity,
            clean_message,
            trace_id,
            span_id,
            clean_fields,
        )
        self._publish(record)
        if removed:
            names = ", ".join(f"{name} ({kind})" for name, kind in removed.items())
            self._notice(f"removed from a {component} log record before saving: {names}")
        return record

    def _notice(self, message: str) -> None:
        """A log line about the filter itself. Built directly so it cannot be filtered again."""
        self._publish(LogRecord(self._next_id(), self._clock_us(), "logging", "WARNING", message))

    def _publish(self, record: LogRecord) -> None:
        self._recent.append(record)
        self._submit(record)
        for listener in list(self._listeners):
            try:
                listener(record)
            except Exception:  # a broken listener must not stop logging  # noqa: S110
                pass

    def debug(self, component: str, message: str, **fields: Any) -> LogRecord | None:
        return self.log("DEBUG", component, message, **fields)

    def info(self, component: str, message: str, **fields: Any) -> LogRecord | None:
        return self.log("INFO", component, message, **fields)

    def warning(self, component: str, message: str, **fields: Any) -> LogRecord | None:
        return self.log("WARNING", component, message, **fields)

    def error(self, component: str, message: str, **fields: Any) -> LogRecord | None:
        return self.log("ERROR", component, message, **fields)

    def critical(self, component: str, message: str, **fields: Any) -> LogRecord | None:
        return self.log("CRITICAL", component, message, **fields)

    # ---- live views ------------------------------------------------------------------------

    def recent(self, count: int = 50) -> list[LogRecord]:
        """The newest ``count`` records, oldest first. Served from memory, never the disk."""
        return list(self._recent)[-count:]

    def remember(self, record: LogRecord) -> None:
        """Add a record made elsewhere (the store's own health lines) to the live list."""
        self._recent.append(record)
        for listener in list(self._listeners):
            try:
                listener(record)
            except Exception:  # noqa: S110
                pass

    def subscribe(self, listener: Callable[[LogRecord], None]) -> None:
        """Call ``listener`` with every new record (used to push logs to the browser)."""
        self._listeners.append(listener)


def record_to_json(record: LogRecord) -> str:
    """The record as one line of JSON."""
    return json.dumps(
        {
            "id": record.id,
            "ts_us": record.ts_us,
            "component": record.component,
            "severity": record.severity,
            "message": record.message,
            "trace_id": record.trace_id,
            "span_id": record.span_id,
            "fields": record.fields,
        },
        default=str,
    )
