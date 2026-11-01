"""Tests for the device event sink (T012)."""

import ast
from pathlib import Path

from deterrent.protocol.codes import DenyReason, RejectCode, State
from deterrent.simulator.events import DeviceEventSink, RecordedEvent, RecordingSink

SIMULATOR_DIR = Path(__file__).resolve().parents[2] / "src" / "deterrent" / "simulator"
FORBIDDEN = {"observability", "api", "composition"}


def imported_packages(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.update(alias.name.split("."))
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.update(node.module.split("."))
    return found


def test_recording_sink_is_a_sink():
    assert isinstance(RecordingSink(), DeviceEventSink)


def test_recording_sink_keeps_events_in_order_with_device_time():
    sink = RecordingSink()
    sink.state_changed(0, State.BOOT, State.HOMING)
    sink.angles_changed(10, 100, 200)
    sink.message_rejected(20, RejectCode.BAD_LENGTH, 0x02)
    sink.homing_drift(3000, 4)
    sink.fire_denied(3010, DenyReason.NOT_ARMED)
    sink.fire_granted(3020)
    sink.effector_on(3020)
    sink.effector_cut(3100, int(DenyReason.PERSON_NOT_CLEAR))
    sink.interlock_event(3100, DenyReason.PERSON_NOT_CLEAR)
    sink.effector_ended(3200)
    sink.effector_stopped_by_host(3300)
    assert [event.name for event in sink.events] == [
        "state_changed",
        "angles_changed",
        "message_rejected",
        "homing_drift",
        "fire_denied",
        "fire_granted",
        "effector_on",
        "effector_cut",
        "interlock_event",
        "effector_ended",
        "effector_stopped_by_host",
    ]
    assert [event.device_ms for event in sink.events] == [
        0,
        10,
        20,
        3000,
        3010,
        3020,
        3020,
        3100,
        3100,
        3200,
        3300,
    ]


def test_recorded_events_carry_their_details():
    sink = RecordingSink()
    sink.state_changed(5, State.DISARMED, State.SCANNING)
    sink.effector_cut(9, 7)
    assert sink.events[0] == RecordedEvent(
        5, "state_changed", {"old": State.DISARMED, "new": State.SCANNING}
    )
    assert sink.events[1].details == {"cause": 7}


def test_helper_to_find_events_by_name():
    sink = RecordingSink()
    sink.effector_on(1)
    sink.effector_ended(2)
    sink.effector_on(3)
    assert [e.device_ms for e in sink.named("effector_on")] == [1, 3]


def test_base_sink_ignores_everything_so_a_device_can_run_without_a_listener():
    sink = DeviceEventSink()
    sink.state_changed(0, State.BOOT, State.HOMING)
    sink.fire_granted(1)


def test_module_imports_no_forbidden_package():
    packages = imported_packages(SIMULATOR_DIR / "events.py")
    assert packages.isdisjoint(FORBIDDEN), packages & FORBIDDEN


def test_no_simulator_module_imports_a_forbidden_package():
    for path in SIMULATOR_DIR.glob("*.py"):
        packages = imported_packages(path)
        assert packages.isdisjoint(FORBIDDEN), (path.name, packages & FORBIDDEN)
