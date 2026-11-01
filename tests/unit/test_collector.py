"""Tests for the health collector (T058). Virtual time, mock sensors, real worker threads."""

import pytest

from deterrent.health.collector import HealthCollector
from deterrent.health.sensors import MockSensorProvider
from deterrent.observability.metrics import MetricsRecorder
from deterrent.observability.store import MetricRecord
from support.sensor_spawn import spawn_and_wait_briefly
from support.virtual_time import VirtualTime

NAMES = ["cpu_load", "gpu_load", "ram_used", "cpu_temp", "gpu_temp"]


class Rig:
    def __init__(self, *, with_metrics: bool = True) -> None:
        self.vt = VirtualTime()
        self.sensors = {
            name: MockSensorProvider(name, float(10 + i)) for i, name in enumerate(NAMES)
        }
        self.sent: list[MetricRecord] = []
        self.spawns: dict[str, int] = {}
        self.metrics = (
            MetricsRecorder(self.sent.append, clock_us=self.now_us) if with_metrics else None
        )

        def counting_spawn(work):
            return spawn_and_wait_briefly(work)

        self.collector = HealthCollector(
            list(self.sensors.values()),
            self.vt.clock,
            self.now_us,
            self.vt.sleep,
            self.metrics,
            spawn=counting_spawn,
        )

    def now_us(self) -> int:
        return self.vt.elapsed_ms * 1000

    def reading(self, name: str):
        return next(r for r in self.collector.readings() if r.name == name)


@pytest.fixture
async def rig():
    r = Rig()
    await r.collector.start()
    yield r
    for sensor in r.sensors.values():
        sensor.set_value(1.0)  # release any hanging read
    await r.collector.stop()


async def test_before_the_first_read_every_slot_is_unavailable():
    r = Rig()
    readings = r.collector.readings()
    assert [x.name for x in readings] == NAMES
    assert all(x.state == "unavailable" and x.value is None for x in readings)
    assert all(x.unit for x in readings)


async def test_all_sensors_refresh_every_second(rig):
    stamps = []
    for _ in range(4):
        await rig.vt.advance(1000)
        stamps.append({name: rig.reading(name).read_at_us for name in NAMES})
    for name in NAMES:
        series = [s[name] for s in stamps]
        assert series == [1_000_000, 2_000_000, 3_000_000, 4_000_000]
    assert rig.reading("cpu_load").value == 10.0 and rig.reading("cpu_load").state == "available"
    assert rig.reading("gpu_temp").unit == "celsius"


async def test_a_new_value_shows_at_the_next_refresh(rig):
    await rig.vt.advance(1000)
    rig.sensors["ram_used"].set_value(77.5)
    await rig.vt.advance(1000)
    assert rig.reading("ram_used").value == 77.5


async def test_a_hanging_gpu_makes_only_gpu_load_unavailable_and_the_rest_stay_on_schedule(rig):
    rig.sensors["gpu_load"].set_hang()
    stamps = []
    for _ in range(6):
        await rig.vt.advance(1000)
        stamps.append(rig.reading("cpu_load").read_at_us)
    assert stamps == [1_000_000 * n for n in range(1, 7)]
    gpu = rig.reading("gpu_load")
    assert gpu.state == "unavailable" and gpu.value is None
    for name in ("cpu_load", "ram_used", "cpu_temp", "gpu_temp"):
        assert rig.reading(name).state == "available"
        assert rig.reading(name).read_at_us == 6_000_000


async def test_a_hanging_sensor_is_given_up_on_exactly_one_second_after_its_read_began(rig):
    await rig.vt.advance(500)  # the read at 0 s finished long ago
    rig.sensors["cpu_temp"].set_hang()
    await rig.vt.advance(1499)  # the read that began at 1 s has been waiting 999 ms
    assert rig.reading("cpu_temp").read_at_us == 0  # nothing new has been published yet
    await rig.vt.advance(1)
    reading = rig.reading("cpu_temp")
    assert reading.read_at_us == 2_000_000  # given up on exactly at 2 s
    assert reading.state == "unavailable" and reading.value is None


async def test_a_hanging_sensor_does_not_pile_up_threads(rig):
    started = []
    original = rig.collector._spawn

    def watching(work):
        started.append(1)
        return original(work)

    rig.collector._spawn = watching
    rig.sensors["gpu_load"].set_hang()
    await rig.vt.advance(10_000)
    # five sensors read ten times, but the hung one is only started once
    assert len(started) <= 5 * 10 - 8


async def test_a_value_that_was_readable_then_fails_becomes_unavailable_at_once(rig):
    await rig.vt.advance(1000)
    assert rig.reading("gpu_temp").value == 14.0
    rig.sensors["gpu_temp"].set_error()
    await rig.vt.advance(1000)
    reading = rig.reading("gpu_temp")
    assert reading.state == "unavailable" and reading.value is None  # not 14.0


async def test_a_sensor_that_returns_nothing_is_unavailable(rig):
    rig.sensors["cpu_temp"].set_value(None)
    await rig.vt.advance(1000)
    assert rig.reading("cpu_temp").state == "unavailable"


async def test_a_sensor_that_returns_a_non_number_is_unavailable(rig):
    rig.sensors["cpu_load"].set_value(float("nan"))
    await rig.vt.advance(1000)
    assert rig.reading("cpu_load").state == "unavailable"
    rig.sensors["cpu_load"].set_value("hot")  # not even a number
    await rig.vt.advance(1000)
    assert rig.reading("cpu_load").state == "unavailable"


async def test_a_failed_sensor_recovers_when_it_works_again(rig):
    rig.sensors["gpu_load"].set_error()
    await rig.vt.advance(2000)
    rig.sensors["gpu_load"].set_value(33.0)
    await rig.vt.advance(1000)
    assert rig.reading("gpu_load").value == 33.0


async def test_a_late_answer_from_a_timed_out_read_is_never_used(rig):
    rig.sensors["gpu_load"].set_hang()
    await rig.vt.advance(2500)  # the read begun at 1 s timed out at 2 s: unavailable
    rig.sensors["gpu_load"].set_value(99.0)  # the stuck read now finishes with 99
    await rig.vt.advance(100)  # the next fresh read is not due until 3 s
    assert rig.reading("gpu_load").state == "unavailable"
    await rig.vt.advance(1000)  # now the next cycle reads fresh
    assert rig.reading("gpu_load").value == 99.0


async def test_gauges_are_saved_every_ten_seconds_for_available_sensors(rig):
    await rig.vt.advance(35_000)
    by_name: dict[str, list[MetricRecord]] = {}
    for record in rig.sent:
        by_name.setdefault(record.name, []).append(record)
    assert sorted(by_name) == sorted(f"laptop_{n}" for n in NAMES)
    for records in by_name.values():
        assert [r.ts_us for r in records] == [10_000_000, 20_000_000, 30_000_000]
        assert all(r.kind == "gauge" for r in records)
    assert {r.unit for r in by_name["laptop_cpu_temp"]} == {"celsius"}
    assert by_name["laptop_cpu_load"][0].value == 10.0


async def test_a_failed_sensor_saves_no_gauge(rig):
    rig.sensors["gpu_load"].set_error()
    await rig.vt.advance(25_000)
    assert not [r for r in rig.sent if r.name == "laptop_gpu_load"]
    assert len([r for r in rig.sent if r.name == "laptop_cpu_load"]) == 2


async def test_no_refresh_is_ever_missed_by_more_than_4_seconds_over_5_minutes(rig):
    rig.sensors["gpu_load"].set_error()  # a broken sensor must not slow the others either
    last = {name: 0 for name in NAMES}
    worst = 0
    for _ in range(300):
        await rig.vt.advance(1000)
        for name in NAMES:
            stamp = rig.reading(name).read_at_us
            worst = max(worst, (rig.vt.elapsed_ms * 1000 - stamp) // 1000)
            last[name] = stamp
    assert worst <= 4
    assert all(stamp == 300_000_000 for stamp in last.values())


async def test_stopping_the_collector_stops_the_reads(rig):
    await rig.vt.advance(2000)
    await rig.collector.stop()
    stamp = rig.reading("cpu_load").read_at_us
    await rig.vt.advance(5000)
    assert rig.reading("cpu_load").read_at_us == stamp


async def test_the_collector_works_without_a_metrics_recorder():
    r = Rig(with_metrics=False)
    await r.collector.start()
    await r.vt.advance(11_000)
    assert r.reading("cpu_load").value == 10.0
    await r.collector.stop()


def test_providers_must_have_distinct_names():
    vt = VirtualTime()
    twice = [MockSensorProvider("cpu_load", 1.0), MockSensorProvider("cpu_load", 2.0)]
    with pytest.raises(ValueError):
        HealthCollector(twice, vt.clock, lambda: 0, vt.sleep, None)
