"""Tests for metrics and percentiles (T048)."""

import math
import random
from fractions import Fraction

import pytest

from deterrent.observability.metrics import (
    REQUIRED_METRICS,
    MetricsRecorder,
    nearest_rank,
    summarize,
)
from deterrent.observability.store import MetricRecord


def independent_percentile(values: list[float], p: int) -> float:
    """Nearest-rank from the definition, using exact fractions so no rounding can creep in."""
    ordered = sorted(values)
    rank = math.ceil(Fraction(p, 100) * len(ordered))
    return ordered[max(rank, 1) - 1]


def make_recorder():
    sent: list[MetricRecord] = []
    return MetricsRecorder(sent.append, clock_us=lambda: 42), sent


# ---- percentiles ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "p50", "p95"),
    [
        ([15, 20, 35, 40, 50], 35, 50),  # the textbook example
        (list(range(1, 101)), 50, 95),
        (list(range(1, 21)), 10, 19),
        ([7, 7, 7], 7, 7),
        ([10, 1], 1, 10),
    ],
)
def test_p50_and_p95_on_fixed_samples(values, p50, p95):
    shuffled = values[:]
    random.Random(1).shuffle(shuffled)
    summary = summarize(shuffled)
    assert (summary.p50, summary.p95, summary.count) == (p50, p95, len(values))


def test_percentiles_match_an_independent_calculation_on_random_samples():
    rng = random.Random(2026)
    for _ in range(300):
        n = rng.randint(1, 120)
        values = [round(rng.uniform(0, 1000), 3) for _ in range(n)]
        summary = summarize(values)
        assert summary.p50 == independent_percentile(values, 50)
        assert summary.p95 == independent_percentile(values, 95)
        assert nearest_rank(sorted(values), 99) == independent_percentile(values, 99)


def test_the_rank_is_ceil_p_over_100_times_n():
    ordered = list(range(1, 11))  # n = 10
    assert nearest_rank(ordered, 50) == 5  # rank ceil(5.0) = 5
    assert nearest_rank(ordered, 51) == 6  # rank ceil(5.1) = 6
    assert nearest_rank(ordered, 95) == 10  # rank ceil(9.5) = 10
    assert nearest_rank(ordered, 100) == 10


def test_no_samples_is_empty_never_zero():
    assert summarize([]) is None
    assert nearest_rank([], 50) is None


def test_one_sample_gives_p50_equal_to_p95():
    summary = summarize([12.5])
    assert summary.p50 == summary.p95 == 12.5
    assert summary.count == 1


def test_percentile_arguments_are_checked():
    with pytest.raises(ValueError):
        nearest_rank([1, 2, 3], 0)
    with pytest.raises(ValueError):
        nearest_rank([1, 2, 3], 101)


def test_the_method_is_named_nearest_rank():
    assert summarize([1]).method == "nearest-rank"


# ---- the required metrics ---------------------------------------------------------------------


def test_every_required_metric_exists_with_its_kind_and_unit():
    expected = {
        "laptop_cpu_load": ("gauge", "percent", ()),
        "laptop_gpu_load": ("gauge", "percent", ()),
        "laptop_ram_used": ("gauge", "percent", ()),
        "laptop_cpu_temp": ("gauge", "celsius", ()),
        "laptop_gpu_temp": ("gauge", "celsius", ()),
        "e2e_latency_simulated": ("sample", "milliseconds", ()),
        "fire_requests_granted": ("counter", "count", ()),
        "fire_requests_denied": ("counter", "count", ("reason",)),
        "interlock_events": ("counter", "count", ("cause",)),
        "protocol_messages_rejected": ("counter", "count", ("cause",)),
        "homing_drift_simulated": ("sample", "degrees", ()),
    }
    assert {n: (s.kind, s.unit, s.labels) for n, s in REQUIRED_METRICS.items()} == expected


# ---- recording --------------------------------------------------------------------------------


def test_counters_record_an_increment_per_label():
    recorder, sent = make_recorder()
    recorder.counter("fire_requests_denied", reason="KEY_OFF")
    recorder.counter("fire_requests_denied", reason="KEY_OFF")
    recorder.counter("fire_requests_denied", reason="NOT_ARMED")
    recorder.counter("fire_requests_granted")
    totals: dict[tuple, float] = {}
    for record in sent:
        key = (record.name, tuple(sorted(record.labels.items())))
        totals[key] = totals.get(key, 0) + record.value
    assert totals[("fire_requests_denied", (("reason", "KEY_OFF"),))] == 2
    assert totals[("fire_requests_denied", (("reason", "NOT_ARMED"),))] == 1
    assert totals[("fire_requests_granted", ())] == 1
    assert all(r.kind == "counter" and r.unit == "count" and r.ts_us == 42 for r in sent)


def test_gauges_and_samples_carry_their_unit_and_kind():
    recorder, sent = make_recorder()
    recorder.gauge("laptop_cpu_temp", 61.5)
    recorder.sample("e2e_latency_simulated", 87.25)
    recorder.sample("homing_drift_simulated", 0.03)
    assert [(r.name, r.kind, r.unit, r.value) for r in sent] == [
        ("laptop_cpu_temp", "gauge", "celsius", 61.5),
        ("e2e_latency_simulated", "sample", "milliseconds", 87.25),
        ("homing_drift_simulated", "sample", "degrees", 0.03),
    ]


def test_the_wrong_kind_for_a_known_name_is_refused():
    recorder, sent = make_recorder()
    with pytest.raises(ValueError):
        recorder.gauge("fire_requests_granted", 1)
    with pytest.raises(ValueError):
        recorder.counter("laptop_cpu_load")
    assert sent == []


def test_labels_must_match_the_metric():
    recorder, sent = make_recorder()
    with pytest.raises(ValueError):
        recorder.counter("fire_requests_denied")  # reason missing
    with pytest.raises(ValueError):
        recorder.counter("fire_requests_denied", cause="KEY_OFF")  # wrong label
    with pytest.raises(ValueError):
        recorder.counter("fire_requests_granted", reason="x")  # no labels allowed
    assert sent == []


def test_a_unit_is_required_for_a_name_that_is_not_in_the_list():
    recorder, sent = make_recorder()
    with pytest.raises(ValueError):
        recorder.record("brand_new_metric", "gauge", 1.0)
    recorder.record("brand_new_metric", "gauge", 1.0, unit="widgets")
    assert sent[0].unit == "widgets"


def test_a_given_unit_may_not_contradict_the_list():
    recorder, _ = make_recorder()
    with pytest.raises(ValueError):
        recorder.record("laptop_cpu_temp", "gauge", 50.0, unit="percent")


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, None, True, "12"])
def test_a_value_must_be_a_real_finite_number(bad):
    recorder, sent = make_recorder()
    with pytest.raises(ValueError):
        recorder.gauge("laptop_cpu_load", bad)
    assert sent == []


def test_an_empty_unit_is_refused_by_the_record_itself():
    recorder, _ = make_recorder()
    with pytest.raises(ValueError):
        recorder.record("brand_new_metric", "sample", 1.0, unit="  ")


def test_the_kind_must_be_counter_gauge_or_sample():
    recorder, _ = make_recorder()
    with pytest.raises(ValueError):
        recorder.record("brand_new_metric", "histogram", 1.0, unit="x")
