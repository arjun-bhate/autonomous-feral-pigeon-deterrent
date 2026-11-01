"""Tests for the laptop health sensors (T057). No real hardware is touched."""

import math
import threading
import time
from types import SimpleNamespace

import pytest

from deterrent.health.sensors import (
    SENSOR_UNITS,
    CpuLoadSensor,
    CpuTempSensor,
    GpuLoadSensor,
    GpuTempSensor,
    HealthReading,
    MockSensorProvider,
    NvmlSource,
    RamUsedSensor,
    default_providers,
)

NAMES = ("cpu_load", "gpu_load", "ram_used", "cpu_temp", "gpu_temp")


# ---- names and units ----


def test_the_five_sensor_names_have_their_documented_units():
    assert SENSOR_UNITS == {
        "cpu_load": "percent",
        "gpu_load": "percent",
        "ram_used": "percent",
        "cpu_temp": "celsius",
        "gpu_temp": "celsius",
    }


def test_the_default_providers_cover_all_five_names_once_each_with_a_unit():
    providers = default_providers()
    assert sorted(p.name for p in providers) == sorted(NAMES)
    assert all(p.unit == SENSOR_UNITS[p.name] for p in providers)


def test_a_reading_needs_a_unit_and_a_known_state():
    ok = HealthReading("cpu_load", 12.5, "percent", "available", 5)
    assert ok.value == 12.5
    with pytest.raises(ValueError):
        HealthReading("cpu_load", 12.5, "", "available", 5)
    with pytest.raises(ValueError):
        HealthReading("cpu_load", 12.5, "percent", "maybe", 5)


def test_an_unavailable_reading_has_no_value_and_an_available_one_must_have_one():
    gone = HealthReading("cpu_temp", None, "celsius", "unavailable", 5)
    assert gone.value is None
    with pytest.raises(ValueError):
        HealthReading("cpu_temp", 40.0, "celsius", "unavailable", 5)
    with pytest.raises(ValueError):
        HealthReading("cpu_temp", None, "celsius", "available", 5)


# ---- the controllable mock ----


def test_the_mock_returns_its_value_and_it_can_change():
    sensor = MockSensorProvider("cpu_load", 10.0)
    assert sensor.read() == 10.0
    sensor.set_value(55.5)
    assert sensor.read() == 55.5
    assert sensor.unit == "percent"


def test_the_mock_can_be_made_to_return_unavailable():
    sensor = MockSensorProvider("gpu_temp", 60.0)
    sensor.set_value(None)
    assert sensor.read() is None


def test_the_mock_can_be_made_to_raise_and_to_recover():
    sensor = MockSensorProvider("ram_used", 30.0)
    sensor.set_error()
    with pytest.raises(RuntimeError):
        sensor.read()
    sensor.set_value(31.0)
    assert sensor.read() == 31.0


def test_the_mock_can_be_made_to_hang_until_released():
    sensor = MockSensorProvider("gpu_load", 5.0)
    sensor.set_hang()
    results: list[float | None] = []
    thread = threading.Thread(target=lambda: results.append(sensor.read()), daemon=True)
    thread.start()
    time.sleep(0.1)
    assert results == [] and thread.is_alive()
    sensor.set_value(7.0)  # changing the mode releases a hanging read
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_the_mock_checks_its_name_and_unit():
    with pytest.raises(ValueError):
        MockSensorProvider("fan_speed", 1.0)
    with pytest.raises(ValueError):
        MockSensorProvider("cpu_temp", 1.0, unit="percent")


# ---- real providers, with the libraries replaced by fakes ----


def fake_psutil(**kw):
    return SimpleNamespace(**kw)


def test_cpu_load_uses_psutil_cpu_percent():
    sensor = CpuLoadSensor(fake_psutil(cpu_percent=lambda interval=None: 42.5))
    assert sensor.read() == 42.5
    assert sensor.unit == "percent"


def test_ram_used_uses_psutil_virtual_memory_percent():
    sensor = RamUsedSensor(fake_psutil(virtual_memory=lambda: SimpleNamespace(percent=61.2)))
    assert sensor.read() == 61.2


@pytest.mark.parametrize("make", [CpuLoadSensor, RamUsedSensor, CpuTempSensor])
def test_a_psutil_that_raises_gives_unavailable_not_an_exception(make):
    def boom(*a, **k):
        raise OSError("sensor gone")

    broken = fake_psutil(cpu_percent=boom, virtual_memory=boom, sensors_temperatures=boom)
    assert make(broken).read() is None


def test_a_missing_temperature_function_gives_unavailable():
    assert CpuTempSensor(fake_psutil()).read() is None  # like psutil on Windows


def test_no_temperature_entries_give_unavailable():
    assert CpuTempSensor(fake_psutil(sensors_temperatures=lambda: {})).read() is None
    assert CpuTempSensor(fake_psutil(sensors_temperatures=lambda: {"x": []})).read() is None


def test_cpu_temperature_prefers_known_cpu_sensors_and_takes_the_hottest_entry():
    entry = lambda value: SimpleNamespace(current=value)  # noqa: E731
    sensors = {
        "nvme": [entry(80.0)],
        "coretemp": [entry(50.0), entry(61.5), entry(55.0)],
    }
    assert CpuTempSensor(fake_psutil(sensors_temperatures=lambda: sensors)).read() == 61.5


def test_cpu_temperature_falls_back_to_any_sensor_when_no_known_cpu_name_exists():
    entry = SimpleNamespace(current=47.0)
    sensors = {"mystery": [entry]}
    assert CpuTempSensor(fake_psutil(sensors_temperatures=lambda: sensors)).read() == 47.0


@pytest.mark.parametrize("bad", [None, math.nan, math.inf, -300.0, 1000.0])
def test_a_silly_temperature_is_unavailable(bad):
    sensors = {"coretemp": [SimpleNamespace(current=bad)]}
    assert CpuTempSensor(fake_psutil(sensors_temperatures=lambda: sensors)).read() is None


@pytest.mark.parametrize("bad", [math.nan, math.inf, -1.0, 100.5])
def test_a_load_outside_0_to_100_is_unavailable(bad):
    assert CpuLoadSensor(fake_psutil(cpu_percent=lambda interval=None: bad)).read() is None
    assert (
        RamUsedSensor(fake_psutil(virtual_memory=lambda: SimpleNamespace(percent=bad))).read()
        is None
    )


class FakeNvml:
    def __init__(self, *, count: int = 1, load: float = 40, temp: float = 61) -> None:
        self.count, self.load, self.temp = count, load, temp
        self.NVML_TEMPERATURE_GPU = 0
        self.init_calls = 0

    def nvmlInit(self):
        self.init_calls += 1

    def nvmlDeviceGetCount(self):
        return self.count

    def nvmlDeviceGetHandleByIndex(self, index):
        return ("gpu", index)

    def nvmlDeviceGetUtilizationRates(self, handle):
        return SimpleNamespace(gpu=self.load)

    def nvmlDeviceGetTemperature(self, handle, kind):
        return self.temp


def test_gpu_load_and_temperature_come_from_nvml():
    source = NvmlSource(lambda: FakeNvml(load=37, temp=64))
    assert GpuLoadSensor(source).read() == 37
    assert GpuTempSensor(source).read() == 64
    assert GpuLoadSensor(source).unit == "percent" and GpuTempSensor(source).unit == "celsius"


def test_nvml_is_started_only_once_for_many_reads():
    nvml = FakeNvml()
    source = NvmlSource(lambda: nvml)
    load = GpuLoadSensor(source)
    for _ in range(5):
        load.read()
    assert nvml.init_calls == 1


def test_a_missing_nvml_library_gives_unavailable():
    def not_installed():
        raise ImportError("no pynvml")

    source = NvmlSource(not_installed)
    assert GpuLoadSensor(source).read() is None
    assert GpuTempSensor(source).read() is None


def test_nvml_failing_to_start_gives_unavailable_and_is_retried_later():
    nvml = FakeNvml()
    calls = {"n": 0}

    def flaky_init():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("driver not loaded")

    nvml.nvmlInit = flaky_init
    source = NvmlSource(lambda: nvml)
    assert GpuLoadSensor(source).read() is None
    assert GpuLoadSensor(source).read() == 40  # the second try works


def test_a_machine_without_a_gpu_gives_unavailable():
    source = NvmlSource(lambda: FakeNvml(count=0))
    assert GpuLoadSensor(source).read() is None


def test_an_nvml_error_while_reading_gives_unavailable():
    nvml = FakeNvml()

    def broken(handle):
        raise RuntimeError("gpu fell off the bus")

    nvml.nvmlDeviceGetUtilizationRates = broken
    source = NvmlSource(lambda: nvml)
    assert GpuLoadSensor(source).read() is None
    assert GpuTempSensor(source).read() == 61  # the other reading is unaffected


@pytest.mark.parametrize("bad", [-5, 101, math.nan])
def test_gpu_values_outside_a_sane_range_are_unavailable(bad):
    assert GpuLoadSensor(NvmlSource(lambda: FakeNvml(load=bad))).read() is None


def test_every_real_provider_has_a_unit_and_never_raises_with_broken_libraries():
    def boom(*a, **k):
        raise RuntimeError("broken")

    for provider in default_providers():
        assert provider.unit
    broken_psutil = fake_psutil(cpu_percent=boom, virtual_memory=boom, sensors_temperatures=boom)
    providers = [
        CpuLoadSensor(broken_psutil),
        RamUsedSensor(broken_psutil),
        CpuTempSensor(broken_psutil),
        GpuLoadSensor(NvmlSource(boom)),
        GpuTempSensor(NvmlSource(boom)),
    ]
    assert [p.read() for p in providers] == [None] * 5


# ---- the failure switch used by the developer tools ----


def test_a_faultable_provider_passes_readings_through_until_told_to_fail():
    from deterrent.health.sensors import FaultableProvider

    inner = MockSensorProvider("cpu_load", 12.0)
    wrapped = FaultableProvider(inner)
    assert (wrapped.name, wrapped.unit, wrapped.read()) == ("cpu_load", "percent", 12.0)
    wrapped.fail()
    with pytest.raises(RuntimeError):
        wrapped.read()
    wrapped.restore()
    assert wrapped.read() == 12.0
    assert wrapped.failed is False


def test_a_faultable_provider_can_hang_until_restored():
    from deterrent.health.sensors import FaultableProvider

    wrapped = FaultableProvider(MockSensorProvider("gpu_temp", 50.0))
    wrapped.fail("hang")
    results: list[float | None] = []
    thread = threading.Thread(target=lambda: results.append(wrapped.read()), daemon=True)
    thread.start()
    time.sleep(0.1)
    assert thread.is_alive() and wrapped.failed is True
    wrapped.restore()
    thread.join(timeout=5)
    assert results == [50.0]


def test_a_faultable_provider_refuses_an_unknown_mode():
    from deterrent.health.sensors import FaultableProvider

    with pytest.raises(ValueError):
        FaultableProvider(MockSensorProvider("cpu_load", 1.0)).fail("melt")
