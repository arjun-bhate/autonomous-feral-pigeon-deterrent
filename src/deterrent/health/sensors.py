"""Laptop health sensors: where the CPU, GPU, memory and temperature numbers come from.

A **sensor provider** is a small object with a ``name``, a ``unit`` and a ``read()``
method that returns one number, or ``None`` meaning "unavailable". The real providers
ask the ``psutil`` library (CPU load, memory, CPU temperature) and NVIDIA's NVML
library (GPU load and temperature). Many laptops cannot report some of these (psutil
cannot read the CPU temperature on Windows; there may be no NVIDIA GPU), so a missing
sensor is normal: the real providers return ``None`` instead of raising an error.

Every real provider also refuses nonsense: a NaN, or a percentage outside 0 to 100,
or a temperature outside -50 to 150 degrees Celsius, is "unavailable" rather than a
number that looks real.

``MockSensorProvider`` is for tests and the developer tools: it can return a value,
return unavailable, raise an error, or hang, so the code that reads sensors can be
proven to cope with each.
"""

import importlib
import math
import threading
from dataclasses import dataclass
from typing import Any, Protocol

SENSOR_UNITS: dict[str, str] = {
    "cpu_load": "percent",
    "gpu_load": "percent",
    "ram_used": "percent",
    "cpu_temp": "celsius",
    "gpu_temp": "celsius",
}
AVAILABLE = "available"
UNAVAILABLE = "unavailable"

_TEMP_RANGE = (-50.0, 150.0)
_CPU_TEMP_SOURCES = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal")
_HANG_LIMIT_S = 30.0  # a mock that hangs lets go after this long, so a test cannot stall forever


@dataclass(frozen=True)
class HealthReading:
    """One reading as shown on the dashboard. ``unavailable`` always has value ``None``."""

    name: str
    value: float | None
    unit: str
    state: str
    read_at_us: int

    def __post_init__(self) -> None:
        if not self.unit:
            raise ValueError("a reading needs a unit")
        if self.state not in (AVAILABLE, UNAVAILABLE):
            raise ValueError(f"state must be {AVAILABLE} or {UNAVAILABLE}")
        if (self.state == AVAILABLE) != (self.value is not None):
            raise ValueError("an available reading needs a value; an unavailable one has none")


class SensorProvider(Protocol):
    name: str
    unit: str

    def read(self) -> float | None:
        """The current value, or None if the sensor cannot be read."""
        ...


def _within(value: Any, low: float, high: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value) if low <= value <= high else None


def _percent(value: Any) -> float | None:
    return _within(value, 0.0, 100.0)


def _celsius(value: Any) -> float | None:
    return _within(value, *_TEMP_RANGE)


class _Named:
    def __init__(self, name: str, unit: str | None = None) -> None:
        if name not in SENSOR_UNITS:
            raise ValueError(f"unknown sensor {name!r}")
        if unit is not None and unit != SENSOR_UNITS[name]:
            raise ValueError(f"{name} is measured in {SENSOR_UNITS[name]}, not {unit}")
        self.name = name
        self.unit = SENSOR_UNITS[name]


# ---- mock ----


class MockSensorProvider(_Named):
    """A sensor the test controls: a value, unavailable (None), an error, or a hang."""

    def __init__(self, name: str, value: float | None = None, unit: str | None = None) -> None:
        super().__init__(name, unit)
        self._lock = threading.Lock()
        self._mode = "value"
        self._value = value
        self._release = threading.Event()

    def set_value(self, value: float | None) -> None:
        with self._lock:
            self._mode, self._value = "value", value
        self._release.set()  # lets a hanging read finish

    def set_error(self) -> None:
        with self._lock:
            self._mode = "error"
        self._release.set()

    def set_hang(self) -> None:
        with self._lock:
            self._mode = "hang"
        self._release.clear()

    def read(self) -> float | None:
        while True:
            with self._lock:
                mode, value = self._mode, self._value
            if mode == "error":
                raise RuntimeError(f"mock sensor {self.name} failed")
            if mode == "value":
                return value
            if not self._release.wait(_HANG_LIMIT_S):
                return None


# ---- real providers ----


def _psutil() -> Any:
    return importlib.import_module("psutil")


class CpuLoadSensor(_Named):
    def __init__(self, psutil_module: Any = None) -> None:
        super().__init__("cpu_load")
        self._psutil = psutil_module if psutil_module is not None else _psutil()
        try:
            self._psutil.cpu_percent(interval=None)  # the first call has nothing to compare with
        except Exception:  # noqa: S110
            pass

    def read(self) -> float | None:
        try:
            return _percent(self._psutil.cpu_percent(interval=None))
        except Exception:
            return None


class RamUsedSensor(_Named):
    def __init__(self, psutil_module: Any = None) -> None:
        super().__init__("ram_used")
        self._psutil = psutil_module if psutil_module is not None else _psutil()

    def read(self) -> float | None:
        try:
            return _percent(self._psutil.virtual_memory().percent)
        except Exception:
            return None


class CpuTempSensor(_Named):
    """The hottest reading of the CPU's own sensor. Not available on every system."""

    def __init__(self, psutil_module: Any = None) -> None:
        super().__init__("cpu_temp")
        self._psutil = psutil_module if psutil_module is not None else _psutil()

    def read(self) -> float | None:
        try:
            sensors = self._psutil.sensors_temperatures()
        except Exception:  # includes AttributeError on systems without this function
            return None
        chosen = [entries for name, entries in sensors.items() if name in _CPU_TEMP_SOURCES]
        if not chosen:
            chosen = list(sensors.values())
        temperatures = [
            value
            for entries in chosen
            for entry in entries
            if (value := _celsius(getattr(entry, "current", None))) is not None
        ]
        return max(temperatures) if temperatures else None


def _load_pynvml() -> Any:
    return importlib.import_module("pynvml")


class NvmlSource:
    """Talks to the NVIDIA driver once and shares the connection between GPU sensors."""

    def __init__(self, loader=_load_pynvml) -> None:
        self._loader = loader
        self._lock = threading.Lock()
        self._nvml: Any = None
        self._handle: Any = None

    def _connect(self) -> bool:
        if self._handle is not None:
            return True
        try:
            nvml = self._loader()
            nvml.nvmlInit()
            if nvml.nvmlDeviceGetCount() < 1:
                return False
            self._handle = nvml.nvmlDeviceGetHandleByIndex(0)
            self._nvml = nvml
            return True
        except Exception:
            self._handle = None
            return False

    def load(self) -> float | None:
        with self._lock:
            if not self._connect():
                return None
            try:
                return _percent(self._nvml.nvmlDeviceGetUtilizationRates(self._handle).gpu)
            except Exception:
                return None

    def temperature(self) -> float | None:
        with self._lock:
            if not self._connect():
                return None
            try:
                kind = self._nvml.NVML_TEMPERATURE_GPU
                return _celsius(self._nvml.nvmlDeviceGetTemperature(self._handle, kind))
            except Exception:
                return None


class GpuLoadSensor(_Named):
    def __init__(self, source: NvmlSource | None = None) -> None:
        super().__init__("gpu_load")
        self._source = source if source is not None else NvmlSource()

    def read(self) -> float | None:
        return self._source.load()


class GpuTempSensor(_Named):
    def __init__(self, source: NvmlSource | None = None) -> None:
        super().__init__("gpu_temp")
        self._source = source if source is not None else NvmlSource()

    def read(self) -> float | None:
        return self._source.temperature()


class FaultableProvider:
    """Wraps a provider so the developer tools can make it fail (error) or hang on demand."""

    def __init__(self, inner: SensorProvider) -> None:
        self._inner = inner
        self.name = inner.name
        self.unit = inner.unit
        self._lock = threading.Lock()
        self._mode = "ok"
        self._release = threading.Event()

    @property
    def failed(self) -> bool:
        return self._mode != "ok"

    @property
    def mode(self) -> str:
        return self._mode

    def fail(self, mode: str = "error") -> None:
        if mode not in ("error", "hang"):
            raise ValueError("mode must be error or hang")
        with self._lock:
            self._mode = mode
        self._release.clear()

    def restore(self) -> None:
        with self._lock:
            self._mode = "ok"
        self._release.set()

    def read(self) -> float | None:
        while True:
            mode = self._mode
            if mode == "error":
                raise RuntimeError(f"sensor {self.name} made to fail")
            if mode == "ok":
                return self._inner.read()
            if not self._release.wait(_HANG_LIMIT_S):
                return None


def default_providers() -> list[SensorProvider]:
    """The five real providers (a GPU or temperature sensor that is missing reads unavailable)."""
    nvml = NvmlSource()
    return [
        CpuLoadSensor(),
        GpuLoadSensor(nvml),
        RamUsedSensor(),
        CpuTempSensor(),
        GpuTempSensor(nvml),
    ]
