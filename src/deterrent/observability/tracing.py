"""Traces: a timeline of one engagement, from the camera frame to the effector.

An *engagement* is one pass through the chain "see a pigeon, decide, tell the device,
the device answers". A **trace** records it as exactly eight **spans** (steps), always
in this order:

    capture, encode, network, inference, decision, command, esp32_ack, effector_action

Each span has a start time, a duration and a status. The rules (data-model.md 1.4):

* Spans do not overlap: a span starts no earlier than the one before it ends. Gaps
  between spans are allowed and count towards the total.
* The total time of the trace is the end of the last span minus the start of the first.
* A completed trace has all eight spans. A step that never happened is kept with the
  status ``NOT_RUN`` (or ``TIMEOUT`` if a reply never came), never left out.
* A made-up timing (this feature has no camera) is flagged ``simulated``.

A trace is built in memory and written to the store once, when it is complete.
"""

import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from deterrent.observability.store import SpanRecord, TraceRecord

STEP_NAMES = (
    "capture",
    "encode",
    "network",
    "inference",
    "decision",
    "command",
    "esp32_ack",
    "effector_action",
)
_POSITION = {name: index for index, name in enumerate(STEP_NAMES, start=1)}


class SpanStatus(StrEnum):
    OK = "OK"
    DENIED = "DENIED"
    TIMEOUT = "TIMEOUT"
    NOT_RUN = "NOT_RUN"


class Outcome(StrEnum):
    GRANTED = "GRANTED"
    DENIED = "DENIED"
    TIMEOUT = "TIMEOUT"
    NO_FIRE_REQUESTED = "NO_FIRE_REQUESTED"
    RUNNING = "RUNNING"


# A step with no duration cannot have finished.
_STATUSES_WITHOUT_DURATION = frozenset({SpanStatus.TIMEOUT, SpanStatus.NOT_RUN})


@dataclass
class Span:
    span_id: str
    trace_id: str
    position: int
    name: str
    started_us: int
    duration_us: int | None
    status: SpanStatus
    simulated: bool
    attrs: dict[str, Any] = field(default_factory=dict)

    @property
    def ended_us(self) -> int:
        return self.started_us + (self.duration_us or 0)


def _placeholder_note(name: str, status: SpanStatus) -> str:
    if name == "effector_action":
        return "no action"
    if status is SpanStatus.TIMEOUT:
        return "no reply"
    if name == "esp32_ack":
        return "no request sent"
    return "not run"


class Trace:
    """One engagement's timeline. Add the spans in order, then call ``complete``."""

    def __init__(self, trace_id: str | None = None, *, simulated: bool = True) -> None:
        self.trace_id = trace_id if trace_id is not None else secrets.token_hex(8)
        self.simulated = simulated
        self.outcome = Outcome.RUNNING
        self.deny_reason: str | None = None
        self._spans: dict[str, Span] = {}
        self._reserved: dict[str, str] = {}
        self._completed = False

    # ---- building ---------------------------------------------------------------------------

    def span_id_for(self, name: str) -> str:
        """The id the span called ``name`` will have. Lets a log line cite a step in progress."""
        if name not in _POSITION:
            raise ValueError(f"{name!r} is not one of the eight steps")
        if name not in self._reserved:
            self._reserved[name] = secrets.token_hex(4)
        return self._reserved[name]

    def add_span(
        self,
        name: str,
        started_us: int,
        duration_us: int | None,
        status: SpanStatus = SpanStatus.OK,
        *,
        simulated: bool = False,
        attrs: Mapping[str, Any] | None = None,
    ) -> Span:
        if self._completed:
            raise ValueError("the trace is complete and cannot be changed")
        if name not in _POSITION:
            raise ValueError(f"{name!r} is not one of the eight steps")
        if name in self._spans:
            raise ValueError(f"step {name!r} was already recorded")
        position = _POSITION[name]
        if any(span.position > position for span in self._spans.values()):
            raise ValueError(f"step {name!r} comes before a step already recorded")
        if duration_us is None and status not in _STATUSES_WITHOUT_DURATION:
            raise ValueError(f"a step with status {status} needs a duration")
        if duration_us is not None and duration_us < 0:
            raise ValueError("a duration cannot be negative")
        last_end = self._last_end()
        if last_end is not None and started_us < last_end:
            raise ValueError(f"step {name!r} would overlap the one before it (starts too early)")
        span = Span(
            self.span_id_for(name),
            self.trace_id,
            position,
            name,
            started_us,
            duration_us,
            SpanStatus(status),
            simulated,
            dict(attrs or {}),
        )
        self._spans[name] = span
        return span

    def _last_end(self) -> int | None:
        return max((span.ended_us for span in self._spans.values()), default=None)

    def complete(
        self,
        outcome: Outcome,
        deny_reason: str | None = None,
        *,
        unfinished: Mapping[str, SpanStatus] | None = None,
    ) -> None:
        """Finish the trace. Steps never recorded are added as NOT_RUN (or as the status
        given for that step in ``unfinished``, for example TIMEOUT for ``esp32_ack``)."""
        if self._completed:
            raise ValueError("the trace is already complete")
        if outcome is Outcome.RUNNING:
            raise ValueError("a trace cannot be completed as RUNNING")
        self._check_outcome(outcome, deny_reason)
        end = self._last_end()
        if end is None:
            raise ValueError("a trace needs at least one recorded step")
        for name in STEP_NAMES:
            if name in self._spans:
                continue
            status = (unfinished or {}).get(name, SpanStatus.NOT_RUN)
            if status not in _STATUSES_WITHOUT_DURATION:
                raise ValueError(f"an unfinished step can only be TIMEOUT or NOT_RUN, not {status}")
            self._spans[name] = Span(
                self.span_id_for(name),
                self.trace_id,
                _POSITION[name],
                name,
                end,
                None,
                status,
                False,
                {"note": _placeholder_note(name, status)},
            )
        self.outcome = outcome
        self.deny_reason = deny_reason
        self._completed = True

    def _check_outcome(self, outcome: Outcome, deny_reason: str | None) -> None:
        if outcome is Outcome.DENIED:
            ack = self._spans.get("esp32_ack")
            if not deny_reason:
                raise ValueError("a DENIED trace needs the deny reason")
            if ack is None or ack.status is not SpanStatus.DENIED:
                raise ValueError("a DENIED trace needs an esp32_ack span with status DENIED")
            if ack.attrs.get("reason") != deny_reason:
                raise ValueError("the esp32_ack span must carry the same reason as the trace")
        elif deny_reason is not None:
            raise ValueError("only a DENIED trace can carry a deny reason")

    # ---- reading ----------------------------------------------------------------------------

    @property
    def spans(self) -> list[Span]:
        return sorted(self._spans.values(), key=lambda span: span.position)

    @property
    def started_us(self) -> int | None:
        return min((span.started_us for span in self._spans.values()), default=None)

    @property
    def ended_us(self) -> int | None:
        return self._last_end() if self._completed else None

    @property
    def total_us(self) -> int | None:
        if not self._completed:
            return None
        return self.ended_us - self.started_us

    def to_record(self) -> TraceRecord:
        """The row (with its spans) that the store writes. Only for a completed trace."""
        if not self._completed:
            raise ValueError("only a completed trace can be stored")
        return TraceRecord(
            self.trace_id,
            self.started_us,
            self.ended_us,
            self.outcome.value,
            self.deny_reason,
            self.simulated,
            tuple(
                SpanRecord(
                    span.span_id,
                    span.trace_id,
                    span.position,
                    span.name,
                    span.started_us,
                    span.duration_us,
                    span.status.value,
                    span.simulated,
                    span.attrs,
                )
                for span in self.spans
            ),
        )
