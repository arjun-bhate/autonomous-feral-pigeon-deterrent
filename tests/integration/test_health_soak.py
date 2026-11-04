"""A five-minute health run with a GPU hang and a temperature outage (T073).

Run it on its own with ``uv run pytest -m slow``.

Time is virtual (a fake clock the test moves), so five minutes pass in seconds, but the
collector, its schedule and its worker threads are the real ones. The five sensors are
mocks the test can break. The timeline, in seconds of simulated time:

* 0 to 60      everything healthy
* 60 to 90     the GPU load sensor hangs (its read never answers)
* 90 to 150    everything healthy again
* 150 to 180   both temperature sensors fail (errors)
* 180 to 300   everything healthy

What must hold the whole time (SC-001, SC-002, SC-003):

* every slot is refreshed at least every 4 seconds (the requirement; in fact every second);
* the slots that are not broken stay exactly on schedule, one refresh per second;
* a broken slot reads "unavailable" with no value, never an old number;
* a slot comes back by itself once its sensor works again;
* a hung sensor is never asked again while it is still stuck (threads do not pile up).
"""

import asyncio

import pytest

from deterrent.health.collector import HealthCollector
from deterrent.health.sensors import MockSensorProvider
from support.sensor_spawn import spawn_and_wait_briefly
from support.virtual_time import VirtualTime

pytestmark = pytest.mark.slow

NAMES = ["cpu_load", "gpu_load", "ram_used", "cpu_temp", "gpu_temp"]
RUN_S = 300
STEP_MS = 100
MAX_GAP_S = 4  # SC-001
HANG = (60, 90)
OUTAGE = (150, 180)
HANGING = {"gpu_load"}
OUTAGE_SENSORS = {"cpu_temp", "gpu_temp"}
HANG_LENGTH = HANG[1] - HANG[0] - 2  # seconds the stuck sensor is not asked again


def broken_between(name: str, second: float) -> bool:
    return (name in HANGING and HANG[0] <= second < HANG[1]) or (
        name in OUTAGE_SENSORS and OUTAGE[0] <= second < OUTAGE[1]
    )


async def run_soak():
    vt = VirtualTime()
    sensors = {name: MockSensorProvider(name, 10.0 + index) for index, name in enumerate(NAMES)}
    spawned = {name: 0 for name in NAMES}

    def counting_spawn(work):
        spawned[work.__self__.name] += 1  # work is the sensor's bound read method
        return spawn_and_wait_briefly(work)

    collector = HealthCollector(
        list(sensors.values()),
        vt.clock,
        lambda: vt.elapsed_ms * 1000,
        vt.sleep,
        None,
        spawn=counting_spawn,
    )
    await collector.start()

    # every sample: (second, {name: (read_at_us, state, value)})
    samples = []
    hung = False
    try:
        for step in range(RUN_S * 1000 // STEP_MS):
            second = step * STEP_MS / 1000
            if second == HANG[0]:
                sensors["gpu_load"].set_hang()
                hung = True
            if second == HANG[1]:
                sensors["gpu_load"].set_value(11.0)
                hung = False
            if second == OUTAGE[0]:
                for name in OUTAGE_SENSORS:
                    sensors[name].set_error()
            if second == OUTAGE[1]:
                for name in OUTAGE_SENSORS:
                    sensors[name].set_value(10.0 + NAMES.index(name))
            await vt.advance(STEP_MS)
            samples.append(
                (
                    second + STEP_MS / 1000,
                    {r.name: (r.read_at_us, r.state, r.value) for r in collector.readings()},
                )
            )
    finally:
        for sensor in sensors.values():
            sensor.set_value(1.0)  # release anything still hanging
        await collector.stop()
    return samples, spawned, hung


@pytest.fixture(scope="module")
def soak():
    return asyncio.run(run_soak())


def refresh_times(samples, name) -> list[int]:
    """The distinct read_at_us values a slot showed, in order (each is one refresh)."""
    seen: list[int] = []
    for _, readings in samples:
        stamp = readings[name][0]
        if stamp and (not seen or stamp != seen[-1]):
            seen.append(stamp)
    return seen


@pytest.mark.parametrize("name", NAMES)
async def test_no_slot_goes_longer_than_four_seconds_without_a_refresh(soak, name):
    samples, _, _ = soak
    times = refresh_times(samples, name)
    gaps = [(b - a) / 1_000_000 for a, b in zip(times, times[1:], strict=False)]
    assert len(times) >= RUN_S - 2, f"{name} refreshed only {len(times)} times in {RUN_S} s"
    assert max(gaps) <= MAX_GAP_S
    assert times[0] <= 2_000_000  # the first reading arrives within two seconds


@pytest.mark.parametrize("name", [n for n in NAMES if n not in HANGING])
async def test_every_other_slot_is_refreshed_exactly_every_second_even_when_it_fails(soak, name):
    # Stricter than SC-001: a failed read (an error) is reported as "unavailable" at once, so
    # the temperature slots keep their one-second schedule through their outage too.
    samples, _, _ = soak
    times = refresh_times(samples, name)
    gaps = {(b - a) for a, b in zip(times, times[1:], strict=False)}
    assert gaps == {1_000_000}, f"{name} had refresh gaps of {sorted(gaps)} microseconds"


async def test_the_hung_slot_has_one_two_second_gap_when_the_hang_begins_and_then_stays_regular(
    soak,
):
    # The read that hangs is given up on after exactly 1 s; that read started a second after
    # the previous refresh, so the slot refreshes 2 s after its last good reading, once.
    samples, _, _ = soak
    times = refresh_times(samples, "gpu_load")
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    assert set(gaps) <= {1_000_000, 2_000_000}
    assert gaps.count(2_000_000) == 1
    assert max(gaps) / 1_000_000 <= MAX_GAP_S


async def test_the_slots_that_are_not_broken_stay_on_schedule_while_another_is(soak):
    samples, _, _ = soak
    checked = 0
    for second, readings in samples:
        for name in NAMES:
            # unbroken now and for the three seconds before (so its own recovery is not counted)
            if second > 2 and not any(broken_between(name, t) for t in (second - 3, second)):
                _, state, value = readings[name]
                assert state == "available" and value is not None, (name, second)
                checked += 1
    assert checked > RUN_S * 10  # plenty of checks really ran
    # and while the GPU was hung, the other four really were being checked
    assert any(
        HANG[0] + 5 < second < HANG[1] and not broken_between("cpu_load", second)
        for second, _ in samples
    )


async def test_a_broken_slot_reads_unavailable_with_no_value_never_an_old_number(soak):
    samples, _, _ = soak
    saw_unavailable = {name: 0 for name in NAMES}
    for second, readings in samples:
        for name in NAMES:
            _, state, value = readings[name]
            # two seconds after a failure began (one second to give up, one to publish) the
            # slot must be unavailable, and it must hold no value at all
            if (
                broken_between(name, second - 2)
                and broken_between(name, second)
                and second - 2 >= (HANG[0] if name in HANGING else OUTAGE[0])
            ):
                assert state == "unavailable" and value is None, (name, second)
                saw_unavailable[name] += 1
    assert saw_unavailable["gpu_load"] > 100
    assert saw_unavailable["cpu_temp"] > 100 and saw_unavailable["gpu_temp"] > 100
    assert saw_unavailable["cpu_load"] == saw_unavailable["ram_used"] == 0


async def test_a_slot_comes_back_by_itself_within_two_seconds_of_its_sensor_recovering(soak):
    samples, _, _ = soak
    for name, end in (("gpu_load", HANG[1]), ("cpu_temp", OUTAGE[1]), ("gpu_temp", OUTAGE[1])):
        after = [r[name] for second, r in samples if end + 2 <= second <= end + 10]
        assert after and all(
            state == "available" and value is not None for _, state, value in after
        )


async def test_a_hung_sensor_is_started_only_once_per_hang_so_threads_do_not_pile_up(soak):
    _, spawned, hung = soak
    assert hung is False
    # healthy sensors are read about once a second for the whole run
    for name in ("cpu_load", "ram_used"):
        assert RUN_S - 2 <= spawned[name] <= RUN_S + 2
    # the GPU sensor was not asked again for the 30 s it was stuck (a few reads, not 30)
    assert spawned["gpu_load"] <= RUN_S - HANG_LENGTH + 3
