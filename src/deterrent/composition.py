"""The one place that connects the parts of the system together.

Every other module depends only on small interfaces. This module is the single
exception: it builds the observability store, logger and metrics, and it builds the
simulated device together with its link, runner and client, then joins them. The
architecture test (T070) checks that no other module imports both the simulator and
the observability code.

The picture, in plain words::

    dev tools / engagement --> DeviceClient --> link --> SimulatorRunner --> Device
                                   ^                                        |
                                   +------ replies and STATUS events <------+

    Device --(DeviceEventSink)--> logs, counters, the drift sample

The device reports what happened by calling methods on a *sink*
(``DeviceEventSinkAdapter`` below); the adapter turns each call into a log line and,
where the data model asks for it, a metric. The laptop learns the device's live state
only from the device's own STATUS messages, kept in memory so that reading it never
waits for a disk.
"""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from deterrent.clock import Clock, SystemClock
from deterrent.config import Settings, load_settings
from deterrent.engagement.simulated import SimulatedEngagement
from deterrent.link.base import DeviceLink
from deterrent.link.client import DeviceClient
from deterrent.link.inprocess import InProcessLink
from deterrent.observability.logging import StructuredLogger
from deterrent.observability.metrics import MetricsRecorder
from deterrent.observability.store import (
    ObservabilityStore,
    StorageWarning,
    default_db_path,
    wall_clock_us,
)
from deterrent.protocol.codes import DenyReason, RejectCode, State
from deterrent.protocol.messages import DeviceMessage, StatusBlock, StatusEvent, StatusRequest
from deterrent.simulator.control import SimulatorControl
from deterrent.simulator.device import Device
from deterrent.simulator.events import DeviceEventSink
from deterrent.simulator.runner import SimulatorRunner
from deterrent.simulator.zone_storage import SqliteZoneStorage

SIMULATED_BOOT_MS = 1000  # how long the simulated device stays in BOOT, so it can be seen
COMPONENT = "simulator"


def _cause_name(code: int) -> str:
    try:
        return DenyReason(code).name
    except ValueError:
        return f"CODE_{code}"


class DeviceEventSinkAdapter(DeviceEventSink):
    """Turns what the device reports into log lines and metrics."""

    def __init__(self, logger: StructuredLogger, metrics: MetricsRecorder) -> None:
        self._log = logger
        self._metrics = metrics

    def state_changed(self, device_ms: int, old: State, new: State) -> None:
        self._log.info(
            COMPONENT,
            f"state changed {old.name} -> {new.name}",
            device_ms=device_ms,
            old=old.name,
            new=new.name,
        )

    def angles_changed(self, device_ms: int, pan: int, tilt: int) -> None:
        self._log.debug(
            COMPONENT,
            f"angles changed to pan {pan} tilt {tilt} (centidegrees)",
            device_ms=device_ms,
            pan_cdeg=pan,
            tilt_cdeg=tilt,
        )

    def message_rejected(self, device_ms: int, cause: RejectCode, msg_type: int | None) -> None:
        self._metrics.counter("protocol_messages_rejected", cause=cause.name)
        self._log.warning(
            COMPONENT,
            f"message rejected: {cause.name}",
            device_ms=device_ms,
            cause=cause.name,
            msg_type=msg_type,
        )

    def homing_drift(self, device_ms: int, drift_cdeg: int) -> None:
        degrees = drift_cdeg / 100
        self._metrics.sample("homing_drift_simulated", degrees)
        self._log.info(
            COMPONENT,
            f"homing finished, simulated drift {degrees:.2f} degrees",
            device_ms=device_ms,
            drift_cdeg=drift_cdeg,
        )

    def effector_on(self, device_ms: int) -> None:
        self._log.info(COMPONENT, "effector on (simulated)", device_ms=device_ms)

    def effector_ended(self, device_ms: int) -> None:
        self._log.info(
            COMPONENT, "effector off: shot reached its maximum time", device_ms=device_ms
        )

    def effector_cut(self, device_ms: int, cause: int) -> None:
        name = _cause_name(cause)
        self._log.warning(COMPONENT, f"effector cut: {name}", device_ms=device_ms, cause=name)

    def effector_stopped_by_host(self, device_ms: int) -> None:
        self._log.info(COMPONENT, "effector off: stopped by the host", device_ms=device_ms)

    def interlock_event(self, device_ms: int, cause: DenyReason) -> None:
        self._metrics.counter("interlock_events", cause=cause.name)
        self._log.warning(
            COMPONENT,
            f"interlock event: {cause.name}",
            device_ms=device_ms,
            cause=cause.name,
        )

    def fire_granted(self, device_ms: int) -> None:
        self._metrics.counter("fire_requests_granted")
        self._log.info(COMPONENT, "fire request granted", device_ms=device_ms)

    def fire_denied(self, device_ms: int, reason: DenyReason) -> None:
        self._metrics.counter("fire_requests_denied", reason=reason.name)
        self._log.info(
            COMPONENT,
            f"fire request denied: {reason.name}",
            device_ms=device_ms,
            reason=reason.name,
        )


class Platform:
    """Everything the program needs, built once and shared by the API and the tools."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        data_dir: Path | str | None = None,
        clock: Clock | None = None,
        wall_clock_us: Callable[[], int] = wall_clock_us,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic_us: Callable[[], int] | None = None,
        start_store_thread: bool = True,
        boot_duration_ms: int = SIMULATED_BOOT_MS,
        link_wrapper: Callable[[DeviceLink], DeviceLink] | None = None,
    ) -> None:
        self.settings = settings if settings is not None else load_settings()
        self.data_dir = Path(data_dir) if data_dir is not None else default_db_path().parent
        self.clock = clock if clock is not None else SystemClock()
        self.wall_clock_us = wall_clock_us
        self._sleep = sleep
        self._monotonic_us = monotonic_us
        self._start_store_thread = start_store_thread
        self._boot_duration_ms = boot_duration_ms
        self._link_wrapper = link_wrapper

        self.store: ObservabilityStore | None = None
        self.logger: StructuredLogger | None = None
        self.metrics: MetricsRecorder | None = None
        self.store_closed = False
        self.engagement: SimulatedEngagement | None = None

        self.device: Device | None = None
        self.client: DeviceClient | None = None
        self._runner: SimulatorRunner | None = None
        self._link: InProcessLink | None = None
        self._control: SimulatorControl | None = None
        self._status: dict[str, Any] | None = None
        self._person_clear = True
        self._device_listeners: list[Callable[[dict | None], None]] = []
        self._storage_listeners: list[Callable[[StorageWarning | None], None]] = []

    @property
    def sleep(self) -> Callable[[float], Awaitable[None]]:
        """The sleep function every loop uses (tests replace it with virtual time)."""
        return self._sleep

    @property
    def started(self) -> bool:
        return self.store is not None and not self.store_closed

    # ---- lifecycle ----

    async def start(self) -> None:
        """Open the observability store and start logging."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = ObservabilityStore(
            self.data_dir / "observability.sqlite3",
            start=self._start_store_thread,
            clock_us=self.wall_clock_us,
        )
        self.logger = StructuredLogger(
            self.store.submit, next_id=self.store.next_log_id, clock_us=self.wall_clock_us
        )
        self.store.on_log = self.logger.remember  # the store's own health lines show up live
        self.store.add_warning_listener(self._on_storage_warning)
        self.metrics = MetricsRecorder(self.store.submit, clock_us=self.wall_clock_us)
        self.store_closed = False
        extra = {} if self._monotonic_us is None else {"monotonic_us": self._monotonic_us}
        self.engagement = SimulatedEngagement(
            lambda: self.client,
            self.logger,
            self.metrics,
            self.store.submit,
            wall_clock_us=self.wall_clock_us,
            **extra,
        )
        self.logger.info("api", "platform started")

    async def stop(self) -> None:
        await self.stop_simulator()
        if self.store is not None and not self.store_closed:
            self.logger.info("api", "platform stopped")
            self.store.close()
            self.store_closed = True

    # ---- the simulated device ----

    @property
    def simulator_running(self) -> bool:
        return self._runner is not None

    async def start_simulator(self) -> None:
        """Boot a new simulated device (it homes by itself and ends DISARMED)."""
        if self.simulator_running:
            raise RuntimeError("the simulator is already running")
        sink = DeviceEventSinkAdapter(self.logger, self.metrics)
        storage = SqliteZoneStorage(self.data_dir / "device" / "zone_store.sqlite3")
        self.device = Device(
            self.clock,
            self.settings,
            sink,
            storage,
            boot_duration_ms=self._boot_duration_ms,
            allow_test_injection=self.settings.dev_tools,
        )
        self._control = SimulatorControl(self.device)
        self._link = InProcessLink()
        host_link: DeviceLink = self._link.host
        if self._link_wrapper is not None:
            host_link = self._link_wrapper(host_link)
        self._runner = SimulatorRunner(self.device, self._link.device, sleep=self._sleep)
        self.client = DeviceClient(
            host_link,
            self.clock,
            self.settings,
            sleep=self._sleep,
            person_clear=lambda: self._person_clear,
            on_event=self._on_device_event,
        )
        await self._runner.start()
        await self.client.start()
        self.logger.info("api", "simulator started")
        # One honest protocol exchange, so the live status exists before anyone asks.
        reply = await self.client.request(StatusRequest())
        block = StatusBlock.decode_payload(reply.data)
        if isinstance(block, StatusBlock):
            self._set_status(block)

    async def stop_simulator(self) -> None:
        """Stop the simulated device. Live values become unavailable."""
        if not self.simulator_running:
            return
        await self.client.stop()
        await self._runner.stop()
        self._runner = None
        self.client = None
        self.device = None
        self._control = None
        self._link = None
        self._status = None
        self.logger.info("api", "simulator stopped")
        for listener in list(self._device_listeners):
            listener(None)

    # ---- inputs the hardware would provide ----

    def set_key(self, on: bool) -> None:
        self._require_simulator().set_key(on)

    def set_person_clear(self, clear: bool) -> None:
        """Stands in for the future detector: the value put in every heartbeat."""
        self._person_clear = bool(clear)

    def corrupt_zone_store(self) -> None:
        """Test input: damage one stored zone. Raises ValueError if no zone is stored."""
        self._require_simulator().inject({"zone_store_corrupt": True})

    def _require_simulator(self) -> SimulatorControl:
        if self._control is None:
            raise RuntimeError("the simulator is not running")
        return self._control

    # ---- live state, kept in memory ----

    def device_status(self) -> dict[str, Any]:
        """The device status for ``/api/device/status``. Reads memory only."""
        if not self.simulator_running or self._status is None:
            return {"running": False}
        return dict(self._status)

    def add_device_listener(self, listener: Callable[[dict | None], None]) -> None:
        """Called with the new status on every STATUS message, and with None when stopped."""
        self._device_listeners.append(listener)

    def _on_device_event(self, message: DeviceMessage) -> None:
        if isinstance(message, StatusEvent):
            self._set_status(message.status)

    def _set_status(self, block: StatusBlock) -> None:
        self._status = {
            "running": True,
            "state": block.state.name,
            "pan_cdeg": block.pan,
            "tilt_cdeg": block.tilt,
            "key_on": block.key_on,
            "armed": block.armed,
            "homed": block.homed,
            "zone_store_valid": block.zone_store_valid,
            "effector_on": block.effector_on,
            "fault_mask": block.fault_mask,
            "zone_count": block.zone_count,
            "updated_us": self.wall_clock_us(),
        }
        for listener in list(self._device_listeners):
            listener(dict(self._status))

    # ---- storage health ----

    @property
    def storage_warning(self) -> StorageWarning | None:
        return self.store.storage_warning if self.store is not None else None

    def set_storage_mode(self, mode: str) -> None:
        """Test hook: make the observability store behave as if full or locked."""
        self.store.set_fault_mode(mode)

    def add_storage_listener(self, listener: Callable[[StorageWarning | None], None]) -> None:
        self._storage_listeners.append(listener)

    def _on_storage_warning(self, warning: StorageWarning | None) -> None:
        for listener in list(self._storage_listeners):
            listener(warning)
