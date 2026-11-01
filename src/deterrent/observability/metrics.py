"""Metrics: numbers that describe how the system behaves over time.

Three kinds, each written as one ``metric_events`` row every time something happens:

* **counter**: how many times something happened. Each row is an increment (usually
  1); the total is the sum of the rows. Example: ``fire_requests_denied`` with the
  label ``reason=KEY_OFF``.
* **gauge**: the latest value of something that goes up and down. Example: laptop CPU
  temperature.
* **sample**: one measurement of something we want a spread for. Example: one
  end-to-end latency. From many samples we compute percentiles.

A **percentile** answers "what value is the p-percent mark of the sorted samples".
We use the **nearest-rank** method, which is simple enough to check by hand: sort the
n samples, take the one at position ``ceil(p / 100 x n)`` counting from 1. So p95 of
20 samples is the 19th smallest. With no samples there is no answer (``None``), never
a zero, because a zero would look like a real, perfect measurement.

Every metric has a unit (milliseconds, percent, ...). The names the system must
produce are listed in ``REQUIRED_METRICS`` with their kind, unit and label names.
"""

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from deterrent.observability.store import MetricRecord

KINDS = ("counter", "gauge", "sample")
PERCENTILE_METHOD = "nearest-rank"


@dataclass(frozen=True)
class MetricSpec:
    name: str
    kind: str
    unit: str
    labels: tuple[str, ...] = ()


REQUIRED_METRICS: dict[str, MetricSpec] = {
    spec.name: spec
    for spec in (
        MetricSpec("laptop_cpu_load", "gauge", "percent"),
        MetricSpec("laptop_gpu_load", "gauge", "percent"),
        MetricSpec("laptop_ram_used", "gauge", "percent"),
        MetricSpec("laptop_cpu_temp", "gauge", "celsius"),
        MetricSpec("laptop_gpu_temp", "gauge", "celsius"),
        MetricSpec("e2e_latency_simulated", "sample", "milliseconds"),
        MetricSpec("fire_requests_granted", "counter", "count"),
        MetricSpec("fire_requests_denied", "counter", "count", ("reason",)),
        MetricSpec("interlock_events", "counter", "count", ("cause",)),
        MetricSpec("protocol_messages_rejected", "counter", "count", ("cause",)),
        MetricSpec("homing_drift_simulated", "sample", "degrees"),
    )
}


# ---- percentiles ------------------------------------------------------------------------------


def nearest_rank(ordered: Sequence[float], percentile: int) -> float | None:
    """The nearest-rank percentile of already-sorted values, or None if there are none."""
    if not 1 <= percentile <= 100:
        raise ValueError("percentile must be between 1 and 100")
    if not ordered:
        return None
    # ceil(p * n / 100) in whole numbers, so there is no floating-point rounding.
    rank = -(-percentile * len(ordered) // 100)
    return ordered[max(rank, 1) - 1]


@dataclass(frozen=True)
class Distribution:
    count: int
    p50: float
    p95: float
    method: str = PERCENTILE_METHOD


def summarize(values: Sequence[float]) -> Distribution | None:
    """Count, p50 and p95 of the samples; None when there are no samples."""
    if not values:
        return None
    ordered = sorted(values)
    return Distribution(len(ordered), nearest_rank(ordered, 50), nearest_rank(ordered, 95))


# ---- recording --------------------------------------------------------------------------------


def _check_value(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError("a metric value must be a finite number")
    return float(value)


class MetricsRecorder:
    """Turns calls such as ``counter(...)`` into ``MetricRecord`` rows for the store."""

    def __init__(self, submit: Callable[[MetricRecord], None], *, clock_us: Callable[[], int]):
        self._submit = submit
        self._clock_us = clock_us

    def counter(self, name: str, increment: int = 1, **labels: Any) -> None:
        self.record(name, "counter", increment, labels=labels)

    def gauge(self, name: str, value: float, **labels: Any) -> None:
        self.record(name, "gauge", value, labels=labels)

    def sample(self, name: str, value: float, **labels: Any) -> None:
        self.record(name, "sample", value, labels=labels)

    def record(
        self,
        name: str,
        kind: str,
        value: float,
        *,
        unit: str | None = None,
        labels: dict[str, Any] | None = None,
    ) -> None:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        number = _check_value(value)
        labels = {key: str(item) for key, item in (labels or {}).items()}
        spec = REQUIRED_METRICS.get(name)
        if spec is not None:
            if kind != spec.kind:
                raise ValueError(f"{name} is a {spec.kind}, not a {kind}")
            if unit is not None and unit != spec.unit:
                raise ValueError(f"{name} is measured in {spec.unit}, not {unit}")
            if set(labels) != set(spec.labels):
                raise ValueError(f"{name} needs exactly the labels {spec.labels}")
            unit = spec.unit
        if unit is None or not unit.strip():
            raise ValueError(f"metric {name} needs a unit")
        self._submit(MetricRecord(self._clock_us(), name, kind, number, unit, labels))
