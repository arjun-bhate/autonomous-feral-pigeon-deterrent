"""Where the simulated device reports what happened.

The device never writes logs or touches a database itself. Whenever something worth
recording happens (a state change, a rejected message, the effector switching on)
it calls a method on a *sink*: an object handed to it at construction. The device
only knows this interface; whoever builds the device decides what the sink does
(keep a list for tests, write logs and counters later). That keeps the device free
of any dependency on the logging, API or wiring code.

``DeviceEventSink`` does nothing by default, so a device can run with no listener.
``RecordingSink`` keeps every event in order, for tests.
"""

from dataclasses import dataclass, field
from typing import Any

from deterrent.protocol.codes import DenyReason, RejectCode, State


class DeviceEventSink:
    """Receives device events. Every method does nothing; override what you need.

    ``device_ms`` is always the device's own clock at the moment of the event.
    """

    def state_changed(self, device_ms: int, old: State, new: State) -> None:
        pass

    def angles_changed(self, device_ms: int, pan: int, tilt: int) -> None:
        pass

    def message_rejected(self, device_ms: int, cause: RejectCode, msg_type: int | None) -> None:
        """A message was rejected (any receiver step). ``msg_type`` is None if unknown."""

    def homing_drift(self, device_ms: int, drift_cdeg: int) -> None:
        """Homing finished; the simulated drift it recorded (metric homing_drift_simulated)."""

    def effector_on(self, device_ms: int) -> None:
        pass

    def effector_ended(self, device_ms: int) -> None:
        """The shot reached its maximum duration (normal end)."""

    def effector_cut(self, device_ms: int, cause: int) -> None:
        """The effector was cut by an interlock. ``cause`` is the permanent denial code."""

    def effector_stopped_by_host(self, device_ms: int) -> None:
        pass

    def interlock_event(self, device_ms: int, cause: DenyReason) -> None:
        """A safety condition lapsed (counted once per lapse)."""

    def fire_granted(self, device_ms: int) -> None:
        pass

    def fire_denied(self, device_ms: int, reason: DenyReason) -> None:
        pass


@dataclass(frozen=True)
class RecordedEvent:
    device_ms: int
    name: str
    details: dict[str, Any] = field(default_factory=dict)


class RecordingSink(DeviceEventSink):
    """Keeps every event, in order, with the device time. Used by tests."""

    def __init__(self) -> None:
        self.events: list[RecordedEvent] = []

    def named(self, name: str) -> list[RecordedEvent]:
        return [event for event in self.events if event.name == name]

    def _record(self, device_ms: int, name: str, **details: Any) -> None:
        self.events.append(RecordedEvent(device_ms, name, details))

    def state_changed(self, device_ms: int, old: State, new: State) -> None:
        self._record(device_ms, "state_changed", old=old, new=new)

    def angles_changed(self, device_ms: int, pan: int, tilt: int) -> None:
        self._record(device_ms, "angles_changed", pan=pan, tilt=tilt)

    def message_rejected(self, device_ms: int, cause: RejectCode, msg_type: int | None) -> None:
        self._record(device_ms, "message_rejected", cause=cause, msg_type=msg_type)

    def homing_drift(self, device_ms: int, drift_cdeg: int) -> None:
        self._record(device_ms, "homing_drift", drift_cdeg=drift_cdeg)

    def effector_on(self, device_ms: int) -> None:
        self._record(device_ms, "effector_on")

    def effector_ended(self, device_ms: int) -> None:
        self._record(device_ms, "effector_ended")

    def effector_cut(self, device_ms: int, cause: int) -> None:
        self._record(device_ms, "effector_cut", cause=cause)

    def effector_stopped_by_host(self, device_ms: int) -> None:
        self._record(device_ms, "effector_stopped_by_host")

    def interlock_event(self, device_ms: int, cause: DenyReason) -> None:
        self._record(device_ms, "interlock_event", cause=cause)

    def fire_granted(self, device_ms: int) -> None:
        self._record(device_ms, "fire_granted")

    def fire_denied(self, device_ms: int, reason: DenyReason) -> None:
        self._record(device_ms, "fire_denied", reason=reason)
