"""The test and operator side of the simulated device.

The real device has a physical key switch that is not a protocol message. In the
simulator, ``SimulatorControl`` is how the outside world turns that key.

It also holds the **test-only injection interface** that scenario vectors need. A
vector such as "armed, but the key is off, and the zone store is corrupt" describes a
situation that normal commands cannot reach (turning the key off from armed sends the
device to SAFE_ERROR). ``apply_initial`` and ``inject`` put the device straight into
such a situation.

Safety rules for this back door:

* It can set inputs and state. It has **no way to switch the effector on or to produce
  a grant**: there is no effector field, the DETER state (which means the effector is
  on) cannot be injected, and a fire request still has to be sent and decided by
  ``decide_fire`` like any other.
* It works only if the device was built with ``allow_test_injection=True``; otherwise
  every call raises ``InjectionDisabled`` and nothing changes.
* Nothing is changed unless the whole request is valid.
"""

from collections.abc import Mapping
from typing import Any

from deterrent.protocol.codes import State
from deterrent.protocol.crc import zone_bytes, zone_crc32
from deterrent.simulator.device import Device
from deterrent.simulator.person import PersonSnapshot
from deterrent.simulator.zone_storage import MockZoneStorage, StorageMode, StoredZone

_U32 = 2**32


class InjectionDisabled(RuntimeError):
    """State injection was attempted on a device that was not built to allow it."""


_SCALAR_FIELDS = {"state", "homed", "armed", "key_on", "pan_cdeg", "tilt_cdeg", "fault_mask"}
_OTHER_FIELDS = {
    "heartbeat_age_ms",
    "no_heartbeat",
    "person_clear",
    "person_clear_age_ms",
    "clear_streak_ms",
    "zones",
    "zone_store_corrupt",
    "zone_storage_unreadable",
    "cooldown_remaining_ms",
    "last_seq",
}
_ALLOWED_FIELDS = _SCALAR_FIELDS | _OTHER_FIELDS


def _parse_state(value: Any) -> State:
    if isinstance(value, State):
        return value
    name = str(value).upper().replace("/", "_")
    try:
        return State[name]
    except KeyError as error:
        raise ValueError(f"unknown state {value!r}") from error


class SimulatorControl:
    def __init__(self, device: Device) -> None:
        self._device = device

    def set_key(self, on: bool) -> None:
        """Turn the arm key on or off. This is a real input change, so the key-off rule runs."""
        self._device.set_key_level(on)

    # ---- the test-only interface -----------------------------------------------------------

    def apply_initial(self, initial: Mapping[str, Any]) -> None:
        """Put the device in a starting situation.

        Anything not named takes the default: booted, homed, disarmed, key off, no zones,
        head at the origin, heartbeat fresh, person clear for longer than the re-engage
        delay, no cooldown, no faults, effector off.
        """
        self._require_enabled()
        self._validate(initial, resets_zones=True)
        settings = self._device.settings
        fields: dict[str, Any] = {
            "state": State.DISARMED,
            "homed": True,
            "armed": False,
            "key_on": False,
            "pan_cdeg": 0,
            "tilt_cdeg": 0,
            "fault_mask": 0,
            "heartbeat_age_ms": 0,
            "person_clear": True,
            "clear_streak_ms": settings.person_reengage_delay_ms + 1,
            "zones": [],
            "zone_store_corrupt": False,
            "zone_storage_unreadable": False,
            "cooldown_remaining_ms": 0,
            "last_seq": None,
        }
        fields["person_clear_age_ms"] = initial.get("heartbeat_age_ms", 0)
        fields.update(initial)
        if fields.get("no_heartbeat"):
            fields.pop("heartbeat_age_ms", None)
        self._device.effector_on = False
        self._device.zones.discard()
        self._apply(fields)

    def inject(self, changes: Mapping[str, Any]) -> None:
        """Change only the named fields, in the middle of a scenario."""
        self._require_enabled()
        self._validate(changes)
        self._apply(dict(changes))

    # ---- checks ------------------------------------------------------------------------------

    def _require_enabled(self) -> None:
        if not self._device.test_injection_allowed:
            raise InjectionDisabled("this device was not built to allow state injection")

    def _validate(self, fields: Mapping[str, Any], resets_zones: bool = False) -> None:
        unknown = set(fields) - _ALLOWED_FIELDS
        if unknown:
            raise ValueError(f"unknown field(s): {sorted(unknown)}")
        if "state" in fields and _parse_state(fields["state"]) is State.DETER:
            raise ValueError("DETER means the effector is on and cannot be injected")
        if fields.get("zone_storage_unreadable") and not isinstance(
            self._device.storage, MockZoneStorage
        ):
            raise ValueError("zone_storage_unreadable needs a storage with failure modes")
        if fields.get("zone_store_corrupt"):
            if "zones" in fields:
                has_zone = bool(fields["zones"])
            else:  # not named: a fresh start has none, a mid-scenario change keeps what is stored
                has_zone = not resets_zones and bool(self._device.storage.read_all())
            if not has_zone:
                raise ValueError("zone_store_corrupt needs at least one stored zone")
        remaining = fields.get("cooldown_remaining_ms")
        if remaining is not None and not 0 <= remaining <= self._device.settings.cooldown_ms:
            raise ValueError("cooldown_remaining_ms must be between 0 and cooldown_ms")

    # ---- applying ------------------------------------------------------------------------------

    def _apply(self, fields: dict[str, Any]) -> None:
        device = self._device
        now = device.now_ms()
        self._apply_storage(fields)
        if "state" in fields:
            state = _parse_state(fields["state"])
            device.state = state
            if state is State.HOMING:
                device._homing_started_ms = now
            if state is State.BOOT:
                device._boot_started_ms = now
        for name, attribute in (("homed", "homed"), ("armed", "armed"), ("key_on", "key_on")):
            if name in fields:
                setattr(device, attribute, bool(fields[name]))  # silent: not a key event
        if "pan_cdeg" in fields:
            device.pan = int(fields["pan_cdeg"])
        if "tilt_cdeg" in fields:
            device.tilt = int(fields["tilt_cdeg"])
        if "fault_mask" in fields:
            device.fault_mask = int(fields["fault_mask"])
        self._apply_heartbeat_and_person(fields, now)
        if "cooldown_remaining_ms" in fields:
            remaining = int(fields["cooldown_remaining_ms"])
            device._cooldown_started_ms = (
                (now - (device.settings.cooldown_ms - remaining)) % _U32 if remaining > 0 else None
            )
        if "last_seq" in fields:
            device._last_seq = fields["last_seq"]
            device._reply_cache.clear()
        device._last_status = None  # so the next tick reports the new situation

    def _apply_storage(self, fields: dict[str, Any]) -> None:
        storage = self._device.storage
        if "zone_storage_unreadable" in fields and isinstance(storage, MockZoneStorage):
            storage.mode = StorageMode.OK  # first make it writable, set the mode last
        if "zones" in fields:
            storage.clear()
            for zone in fields["zones"]:
                points = [tuple(point) for point in zone["points"]]
                storage.write_zone(
                    StoredZone(
                        zone["zone_id"],
                        zone_crc32(zone["zone_id"], points),
                        len(points),
                        zone_bytes(zone["zone_id"], points)[2:],
                    )
                )
        if fields.get("zone_store_corrupt"):
            for zone in storage.read_all():
                damaged = bytes([zone.points[0] ^ 0x01]) + zone.points[1:]
                storage.write_zone(StoredZone(zone.zone_id, zone.crc32, zone.point_count, damaged))
        if fields.get("zone_storage_unreadable") and isinstance(storage, MockZoneStorage):
            storage.mode = StorageMode.UNREADABLE

    def _apply_heartbeat_and_person(self, fields: dict[str, Any], now: int) -> None:
        device = self._device
        if fields.get("no_heartbeat"):
            device._heartbeat_seen = False
            device._person.restore(PersonSnapshot(None, False, None))
            return
        if "heartbeat_age_ms" in fields:
            device._heartbeat_seen = True
            device._last_heartbeat_ms = (now - int(fields["heartbeat_age_ms"])) % _U32
        if not {"person_clear", "person_clear_age_ms", "clear_streak_ms"} & set(fields):
            return
        current = device.person_snapshot()
        last_rx = current.last_rx_ms
        person_clear = current.person_clear
        clear_since = current.clear_since_ms
        if "person_clear" in fields:
            person_clear = bool(fields["person_clear"])
        if "person_clear_age_ms" in fields:
            last_rx = (now - int(fields["person_clear_age_ms"])) % _U32
        if "clear_streak_ms" in fields:
            clear_since = (now - int(fields["clear_streak_ms"])) % _U32
        if not person_clear:
            clear_since = None
        device._person.restore(PersonSnapshot(last_rx, person_clear, clear_since))
