"""Tests for the read queries (T049): filters, paging, traces, metric summaries."""

import itertools
import math
import random
from fractions import Fraction
from pathlib import Path

import pytest

from deterrent.observability import queries
from deterrent.observability.store import (
    LogRecord,
    MetricRecord,
    ObservabilityStore,
    SpanRecord,
    TraceRecord,
)
from deterrent.observability.tracing import STEP_NAMES

TRACE_A = "a" * 16
TRACE_B = "b" * 16


@pytest.fixture
def store(tmp_path: Path):
    s = ObservabilityStore(tmp_path / "obs.sqlite3", start=False)
    yield s
    s.close()


@pytest.fixture
def conn(store):
    connection = store.open_read_connection()
    yield connection
    connection.close()


def add_log(store, ts_us, component="api", severity="INFO", trace_id=None, message="m"):
    record = LogRecord(store.next_log_id(), ts_us, component, severity, message, trace_id)
    store.submit(record)
    return record


def add_trace(store, trace_id, started_us, outcome="GRANTED", deny_reason=None):
    spans = tuple(
        SpanRecord(f"{trace_id[:4]}{n:04x}", trace_id, n, name, started_us + n * 10, 5, "OK", False)
        for n, name in reversed(list(enumerate(STEP_NAMES, start=1)))  # stored out of order
    )
    store.submit(
        TraceRecord(trace_id, started_us, started_us + 90, outcome, deny_reason, True, spans)
    )


# ---- log filters --------------------------------------------------------------------------------


def test_each_filter_alone_and_combined_returns_exactly_the_matching_entries(store, conn):
    combos = list(
        itertools.product(
            ["api", "simulator", "health"], ["INFO", "ERROR"], [None, TRACE_A, TRACE_B]
        )
    )
    entries = [
        add_log(store, 1000 + index, component, severity, trace)
        for index, (component, severity, trace) in enumerate(combos)
    ]
    store.flush()
    cases = itertools.product([None, "api", "health"], [None, "ERROR"], [None, TRACE_A])
    checked = 0
    for component, severity, trace_id in cases:
        expected = [
            e.id
            for e in entries
            if (component is None or e.component == component)
            and (severity is None or e.severity == severity)
            and (trace_id is None or e.trace_id == trace_id)
        ]
        page = queries.query_logs(conn, component=component, severity=severity, trace_id=trace_id)
        assert [item.id for item in page.items] == expected
        assert page.has_more is False
        checked += 1
    assert checked == 12


def test_no_match_returns_an_empty_list_not_an_error(store, conn):
    add_log(store, 1, "api")
    store.flush()
    page = queries.query_logs(conn, component="nothing_here")
    assert page.items == [] and page.has_more is False


def test_log_items_carry_every_documented_field(store, conn):
    store.submit(
        LogRecord(
            store.next_log_id(), 5, "simulator", "WARNING", "hi", TRACE_A, "12345678", {"a": 1}
        )
    )
    store.flush()
    item = queries.query_logs(conn).items[0]
    assert (item.component, item.severity, item.message) == ("simulator", "WARNING", "hi")
    assert (item.trace_id, item.span_id, item.fields) == (TRACE_A, "12345678", {"a": 1})


def test_a_trace_filter_returns_time_order(store, conn):
    add_log(store, 300, trace_id=TRACE_A, message="third")
    add_log(store, 100, trace_id=TRACE_A, message="first")
    add_log(store, 200, trace_id=TRACE_A, message="second")
    add_log(store, 150, trace_id=TRACE_B, message="other")
    store.flush()
    page = queries.query_logs(conn, trace_id=TRACE_A)
    assert [i.message for i in page.items] == ["first", "second", "third"]


def test_the_component_list_is_sorted_and_distinct(store, conn):
    for component in ("simulator", "api", "api", "health"):
        add_log(store, 1, component)
    store.flush()
    assert queries.list_components(conn) == ["api", "health", "simulator"]
    assert queries.list_components(store.open_read_connection()) == [
        "api",
        "health",
        "simulator",
    ]


# ---- paging -------------------------------------------------------------------------------------


def test_paging_with_before_id_has_no_gaps_and_no_repeats(store, conn):
    entries = [add_log(store, 1000 + i) for i in range(25)]
    store.flush()
    seen: list[int] = []
    before = None
    pages = 0
    while True:
        page = queries.query_logs(conn, limit=10, before_id=before)
        pages += 1
        ids = [item.id for item in page.items]
        assert ids == sorted(ids)  # oldest to newest inside a page
        seen = ids + seen
        if not page.has_more:
            break
        before = ids[0]
    assert pages == 3
    assert seen == [e.id for e in entries]


def test_the_first_page_is_the_newest_entries(store, conn):
    entries = [add_log(store, i) for i in range(30)]
    store.flush()
    page = queries.query_logs(conn, limit=5)
    assert [i.id for i in page.items] == [e.id for e in entries[-5:]]
    assert page.has_more is True


def test_paging_combines_with_filters(store, conn):
    wanted = [add_log(store, i, "health") for i in range(0, 40, 2)]
    for i in range(1, 40, 2):
        add_log(store, i, "api")
    store.flush()
    first = queries.query_logs(conn, component="health", limit=8)
    second = queries.query_logs(conn, component="health", limit=8, before_id=first.items[0].id)
    ids = [i.id for i in second.items] + [i.id for i in first.items]
    assert ids == [e.id for e in wanted[-16:]]


def test_the_default_limit_is_200_and_the_maximum_1000(store, conn):
    for i in range(1100):
        add_log(store, i)
    store.flush()
    assert queries.DEFAULT_LOG_LIMIT == 200 and queries.MAX_LOG_LIMIT == 1000
    default = queries.query_logs(conn)
    assert len(default.items) == 200 and default.has_more is True
    biggest = queries.query_logs(conn, limit=5000)
    assert len(biggest.items) == 1000 and biggest.has_more is True


def test_a_limit_below_one_is_refused(conn):
    for bad in (0, -3):
        with pytest.raises(ValueError):
            queries.query_logs(conn, limit=bad)


# ---- traces -------------------------------------------------------------------------------------


def test_a_trace_lookup_returns_the_spans_in_position_order(store, conn):
    add_trace(store, TRACE_A, 1000, "DENIED", "KEY_OFF")
    store.flush()
    detail = queries.get_trace(conn, TRACE_A)
    assert [s.position for s in detail.spans] == list(range(1, 9))
    assert [s.name for s in detail.spans] == list(STEP_NAMES)
    assert detail.outcome == "DENIED" and detail.deny_reason == "KEY_OFF"
    assert detail.total_us == 90 and detail.simulated is True
    assert detail.spans[0].simulated is False


def test_an_unknown_trace_id_is_not_found(conn):
    assert queries.get_trace(conn, "f" * 16) is None


def test_traces_are_listed_newest_first_with_a_limit(store, conn):
    for index, trace_id in enumerate(["1" * 16, "2" * 16, "3" * 16]):
        add_trace(store, trace_id, 1000 * (index + 1))
    store.flush()
    listed = queries.list_traces(conn)
    assert [t.trace_id for t in listed] == ["3" * 16, "2" * 16, "1" * 16]
    assert listed[0].total_us == 90
    assert [t.trace_id for t in queries.list_traces(conn, limit=2)] == ["3" * 16, "2" * 16]
    with pytest.raises(ValueError):
        queries.list_traces(conn, limit=0)


# ---- metrics ------------------------------------------------------------------------------------


def add_metric(store, ts, name, kind, value, unit, labels=None):
    store.submit(MetricRecord(ts, name, kind, value, unit, labels or {}))


def test_counter_totals_are_summed_by_labels(store, conn):
    for _ in range(3):
        add_metric(store, 1, "fire_requests_denied", "counter", 1, "count", {"reason": "KEY_OFF"})
    add_metric(store, 2, "fire_requests_denied", "counter", 1, "count", {"reason": "NOT_ARMED"})
    add_metric(store, 3, "fire_requests_granted", "counter", 1, "count")
    store.flush()
    counters = {
        (c.name, tuple(sorted(c.labels.items()))): (c.total, c.unit)
        for c in queries.metric_summary(conn).counters
    }
    assert counters == {
        ("fire_requests_denied", (("reason", "KEY_OFF"),)): (3, "count"),
        ("fire_requests_denied", (("reason", "NOT_ARMED"),)): (1, "count"),
        ("fire_requests_granted", ()): (1, "count"),
    }


def test_the_latest_gauge_value_wins(store, conn):
    add_metric(store, 100, "laptop_cpu_load", "gauge", 10.0, "percent")
    add_metric(store, 300, "laptop_cpu_load", "gauge", 30.0, "percent")
    add_metric(store, 200, "laptop_cpu_load", "gauge", 20.0, "percent")
    add_metric(store, 50, "laptop_ram_used", "gauge", 55.0, "percent")
    store.flush()
    gauges = {g.name: (g.value, g.unit, g.read_at_us) for g in queries.metric_summary(conn).gauges}
    assert gauges == {
        "laptop_cpu_load": (30.0, "percent", 300),
        "laptop_ram_used": (55.0, "percent", 50),
    }


def test_distributions_report_count_p50_p95_by_nearest_rank(store, conn):
    rng = random.Random(7)
    values = [round(rng.uniform(1, 500), 2) for _ in range(40)]
    for index, value in enumerate(values):
        add_metric(store, index, "e2e_latency_simulated", "sample", value, "milliseconds")
    store.flush()
    (dist,) = queries.metric_summary(conn).distributions
    ordered = sorted(values)

    def expected(p: int) -> float:
        return ordered[math.ceil(Fraction(p, 100) * len(ordered)) - 1]

    assert (dist.name, dist.unit, dist.count) == ("e2e_latency_simulated", "milliseconds", 40)
    assert (dist.p50, dist.p95) == (expected(50), expected(95))
    assert dist.method == "nearest-rank"


def test_a_distribution_with_no_samples_is_left_out(store, conn):
    add_metric(store, 1, "laptop_cpu_load", "gauge", 5.0, "percent")
    store.flush()
    summary = queries.metric_summary(conn)
    assert summary.distributions == []
    assert queries.metric_summary(store.open_read_connection()).counters == []


def test_an_empty_database_gives_empty_summaries(conn):
    summary = queries.metric_summary(conn)
    assert (summary.counters, summary.gauges, summary.distributions) == ([], [], [])


def test_a_series_returns_time_value_points_for_a_chart(store, conn):
    add_metric(store, 300, "laptop_cpu_load", "gauge", 3.0, "percent")
    add_metric(store, 100, "laptop_cpu_load", "gauge", 1.0, "percent")
    add_metric(store, 200, "laptop_cpu_load", "gauge", 2.0, "percent")
    store.flush()
    series = queries.metric_series(conn, "laptop_cpu_load")
    assert series.unit == "percent"
    assert series.points == [[100, 1.0], [200, 2.0], [300, 3.0]]
    assert queries.metric_series(conn, "laptop_cpu_load", since_us=200).points == [
        [200, 2.0],
        [300, 3.0],
    ]


def test_a_series_can_be_limited_to_one_label_set(store, conn):
    add_metric(store, 1, "fire_requests_denied", "counter", 1, "count", {"reason": "KEY_OFF"})
    add_metric(store, 2, "fire_requests_denied", "counter", 1, "count", {"reason": "NOT_ARMED"})
    store.flush()
    series = queries.metric_series(conn, "fire_requests_denied", labels={"reason": "NOT_ARMED"})
    assert series.points == [[2, 1.0]]


def test_a_series_for_a_known_name_without_data_is_empty_but_has_its_unit(conn):
    series = queries.metric_series(conn, "laptop_cpu_temp")
    assert series.unit == "celsius" and series.points == []


def test_a_series_for_an_unknown_name_is_not_found(conn):
    assert queries.metric_series(conn, "no_such_metric") is None
