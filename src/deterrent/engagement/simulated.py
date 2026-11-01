"""A developer-triggered, simulated engagement.

An *engagement* is one pass through "see a pigeon, decide to deter it, ask the device,
the device answers". There is no camera yet, so this module plays the first steps with
made-up timings and then does the real part: it sends one FIRE_REQUEST through the
``DeviceClient`` and records what the **device** answered.

It records one trace of eight spans (see ``tracing.py``):

* ``capture``, ``encode``, ``network``, ``inference``: made-up durations, flagged
  ``simulated``.
* ``decision``: measured. The engagement decides whether to ask the device at all
  (``fire`` true or false).
* ``command``: measured. Preparing the request and handing it to the client.
* ``esp32_ack``: measured. The time until the device replied, and how it replied:
  ``OK`` (granted), ``DENIED`` (with the reason name), ``REJECTED`` (the device refused
  the message itself, with the reject code name, for example ``FIELD_RANGE`` for a
  shot duration below 500 ms, or ``NOT_HOMED`` in BOOT) or ``TIMEOUT`` (no reply).
* ``effector_action``: ``OK`` only after a grant, marking that the device started the
  effector (the shot's own length is recorded as ``requested_duration_ms``; the device
  ends the shot). After anything else it is ``NOT_RUN`` with the note "no action".

The span times form one timeline: each step starts where the previous one ended, so
the made-up steps push the real ones later and the total is the sum of all steps.

**This module never decides a fire.** The outcome is read from the device's reply and
from nothing else. Anything that is not a GRANTED reply is never reported as a grant.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from deterrent.link.client import CommandTimeout
from deterrent.observability.logging import StructuredLogger, trace_context
from deterrent.observability.metrics import MetricsRecorder
from deterrent.observability.store import Record
from deterrent.observability.tracing import Outcome, SpanStatus, Trace
from deterrent.protocol.codes import DenyReason, RejectCode, Result
from deterrent.protocol.messages import GenericReject, Reply, unpack_granted_on_ms

COMPONENT = "engagement"


class SimulatorNotRunning(RuntimeError):
    """A fire engagement needs the simulated device, and it is not running."""


@dataclass(frozen=True)
class SimulatedTimings:
    """The made-up durations of the steps before the decision, in microseconds."""

    capture_us: int = 12_000
    encode_us: int = 4_000
    network_us: int = 2_000
    inference_us: int = 30_000


class FireClient(Protocol):
    async def fire_request(
        self, intended_pan: int, intended_tilt: int, duration_ms: int, ttl_ms: int | None = None
    ) -> Reply | GenericReject: ...


def _monotonic_us() -> int:
    return time.perf_counter_ns() // 1000


def _name(enum_type, code: int) -> str:
    try:
        return enum_type(code).name
    except ValueError:
        return f"CODE_{code}"


class SimulatedEngagement:
    def __init__(
        self,
        client_provider: Callable[[], FireClient | None],
        logger: StructuredLogger,
        metrics: MetricsRecorder,
        submit: Callable[[Record], None],
        *,
        wall_clock_us: Callable[[], int],
        monotonic_us: Callable[[], int] = _monotonic_us,
        timings: SimulatedTimings | None = None,
    ) -> None:
        self._client_provider = client_provider
        self._logger = logger
        self._metrics = metrics
        self._submit = submit
        self._wall_clock_us = wall_clock_us
        self._monotonic_us = monotonic_us
        self._timings = timings if timings is not None else SimulatedTimings()

    async def run(self, *, fire: bool, duration_ms: int = 500) -> str:
        """Run one engagement and return its trace id."""
        client = self._client_provider()
        if fire and client is None:
            raise SimulatorNotRunning("start the simulator before running a fire engagement")
        trace = Trace(simulated=True)
        cursor = self._wall_clock_us()
        with trace_context(trace.trace_id):
            self._logger.info(COMPONENT, f"engagement started (fire={fire})", fire=fire)
            for name, duration in (
                ("capture", self._timings.capture_us),
                ("encode", self._timings.encode_us),
                ("network", self._timings.network_us),
                ("inference", self._timings.inference_us),
            ):
                trace.add_span(name, cursor, duration, SpanStatus.OK, simulated=True)
                cursor += duration

            cursor += self._measure(trace, "decision", cursor, lambda: None)
            if not fire:
                return self._finish(trace, Outcome.NO_FIRE_REQUESTED, record_latency=False)

            def prepare() -> None:
                self._logger.info(
                    COMPONENT,
                    "sending a fire request to the device",
                    span_id=trace.span_id_for("command"),
                    duration_ms=duration_ms,
                )

            cursor += self._measure(trace, "command", cursor, prepare)

            ack_id = trace.span_id_for("esp32_ack")
            started = self._monotonic_us()
            try:
                reply = await client.fire_request(0, 0, duration_ms)
            except CommandTimeout:
                waited = self._monotonic_us() - started
                trace.add_span(
                    "esp32_ack",
                    cursor,
                    waited,
                    SpanStatus.TIMEOUT,
                    attrs={"note": "no reply"},
                )
                self._logger.error(
                    COMPONENT, "the device did not reply", span_id=ack_id, waited_us=waited
                )
                return self._finish(trace, Outcome.TIMEOUT, record_latency=False)
            waited = self._monotonic_us() - started
            return self._record_reply(trace, reply, cursor, waited, ack_id, duration_ms)

    # ---- helpers ----

    def _measure(self, trace: Trace, name: str, start_us: int, work: Callable[[], Any]) -> int:
        """Run ``work``, record its measured duration as the span ``name``, return the duration."""
        started = self._monotonic_us()
        work()
        duration = max(0, self._monotonic_us() - started)
        trace.add_span(name, start_us, duration, SpanStatus.OK)
        return duration

    def _record_reply(
        self,
        trace: Trace,
        reply: Reply | GenericReject,
        cursor: int,
        waited: int,
        ack_id: str,
        duration_ms: int,
    ) -> str:
        """Turn the device's reply into the esp32_ack span and the outcome. Reads only the reply."""
        if isinstance(reply, Reply) and reply.result is Result.GRANTED:
            granted_on = unpack_granted_on_ms(reply.data)
            trace.add_span("esp32_ack", cursor, waited, SpanStatus.OK, attrs={"result": "GRANTED"})
            self._logger.info(COMPONENT, "the device granted the request", span_id=ack_id)
            cursor += waited
            action_id = trace.span_id_for("effector_action")
            self._measure_action(trace, cursor, action_id, granted_on, duration_ms)
            return self._finish(trace, Outcome.GRANTED)
        if isinstance(reply, Reply) and reply.result is Result.DENIED:
            reason = _name(DenyReason, reply.code)
            trace.add_span("esp32_ack", cursor, waited, SpanStatus.DENIED, attrs={"reason": reason})
            self._logger.info(
                COMPONENT, f"the device denied the request: {reason}", span_id=ack_id, reason=reason
            )
            return self._finish(trace, Outcome.DENIED, deny_reason=reason)
        # Anything else is not a grant: a REJECTED reply, a generic reject, or a reply that
        # should never be sent for a fire request. All are reported as REJECTED.
        if isinstance(reply, GenericReject):
            code = RejectCode.UNKNOWN_TYPE.name
        elif reply.result is Result.REJECTED:
            code = _name(RejectCode, reply.code)
        else:
            code = "UNEXPECTED_REPLY"
        trace.add_span(
            "esp32_ack", cursor, waited, SpanStatus.REJECTED, attrs={"reject_code": code}
        )
        self._logger.warning(
            COMPONENT,
            f"the device rejected the request: {code}",
            span_id=ack_id,
            reject_code=code,
        )
        return self._finish(trace, Outcome.REJECTED)

    def _measure_action(
        self, trace: Trace, start_us: int, span_id: str, granted_on_ms: int, duration_ms: int
    ) -> None:
        started = self._monotonic_us()
        self._logger.info(
            COMPONENT,
            "the device started the effector (simulated)",
            span_id=span_id,
            granted_on_ms=granted_on_ms,
        )
        duration = max(0, self._monotonic_us() - started)
        trace.add_span(
            "effector_action",
            start_us,
            duration,
            SpanStatus.OK,
            attrs={"granted_on_ms": granted_on_ms, "requested_duration_ms": duration_ms},
        )

    def _finish(
        self,
        trace: Trace,
        outcome: Outcome,
        *,
        deny_reason: str | None = None,
        record_latency: bool = True,
    ) -> str:
        trace.complete(outcome, deny_reason)
        self._submit(trace.to_record())
        # A latency sample only means something when the whole chain answered. A timeout
        # would add the wait for a reply that never came, and a "no fire" run never asked.
        if record_latency:
            self._metrics.sample("e2e_latency_simulated", trace.total_us / 1000)
        self._logger.info(
            COMPONENT,
            f"engagement finished: {outcome.value}",
            outcome=outcome.value,
            total_us=trace.total_us,
        )
        return trace.trace_id
