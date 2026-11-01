"""The scenario-vector runner: plays a vector against a device adapter and checks it.

A *vector* (see ``specs/001-platform-core/contracts/scenario-vectors.md``) says "start
like this, send these messages at these times, and the device must answer and end up
like this". This runner is written once and owns everything that is the same for every
device: reading the vector, turning readable messages into wire bytes with the reference
encoder, turning reason names into their permanent numbers, comparing, and the report.
A device (the simulator now, the firmware later) only supplies an *adapter*.

How a run goes:

1. The vector is checked against ``protocol/vector-schema.json`` and its names are
   resolved. A vector that writes a reason number, or names a reason that does not
   exist, is refused with ``InvalidVector`` before anything runs.
2. ``adapter.reset(config)`` and ``adapter.apply_initial(initial)`` build the starting
   situation.
3. The steps are put in time order (a scheduled heartbeat goes before an explicit step
   at the same instant). For each step the adapter moves device time to the step's
   ``at_ms`` (ticking the device on the way), then the step is performed.
4. Every frame the device sends is decoded. Two things are checked for every frame of
   every vector: a DENIED reply must not carry the retired code 13, and an effector
   cut must not carry cause 13.
5. The first mismatch ends the run and is reported with the vector id, the step and
   what was expected and what was seen.

Defaults the runner fills in when a vector leaves a message field out: a FIRE_REQUEST
takes ``issued_at_ms`` = the time of the step, ``ttl_ms`` 500, ``duration_ms`` 500 and
intended angles 0; a HEARTBEAT takes ``person_clear`` true and ``laptop_ts_ms`` 0.
"""

import dataclasses
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from deterrent.protocol.codes import (
    EffectorEvent,
    Fault,
    RejectCode,
    Result,
)
from deterrent.protocol.crc import zone_crc32
from deterrent.protocol.framing import Frame, FrameError, parse_frame
from deterrent.protocol.messages import (
    Arm,
    Disarm,
    EffectorEventMessage,
    FireRequest,
    FireStop,
    GenericReject,
    Heartbeat,
    Home,
    Jog,
    Move,
    RejectNotice,
    Reply,
    StatusBlock,
    StatusRequest,
    ZoneBegin,
    ZoneClear,
    ZoneCommit,
    ZoneDelete,
    ZoneList,
    ZonePoint,
    decode_device_message,
    host_frame,
    unpack_angles,
    unpack_fault_mask,
    unpack_granted_on_ms,
    unpack_heartbeat_data,
    unpack_zone_list,
)

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "protocol" / "vector-schema.json"
REASON_CODES_PATH = ROOT / "protocol" / "reason-codes.json"
VECTOR_DIR = ROOT / "protocol" / "vectors"

RETIRED_CODE = 13
_DEFAULT_TTL_MS = 500
_DEFAULT_DURATION_MS = 500
_SEQ_MODULUS = 65536

_MESSAGE_CLASSES = {
    cls.TYPE.name: cls
    for cls in (
        Home,
        Move,
        Jog,
        Heartbeat,
        Arm,
        Disarm,
        ZoneBegin,
        ZonePoint,
        ZoneCommit,
        ZoneDelete,
        ZoneList,
        ZoneClear,
        FireRequest,
        FireStop,
        StatusRequest,
    )
}
_EVENT_NAMES = {
    "on": EffectorEvent.ON,
    "normal_end": EffectorEvent.NORMAL_END,
    "cut": EffectorEvent.CUT,
    "stopped_by_host": EffectorEvent.STOPPED_BY_HOST,
}
_TRIGGER_ACTIONS = ("send", "send_raw", "set_key", "inject")


class InvalidVector(Exception):
    """The vector itself is wrong (not a device failure): it is refused, not run."""


class DeviceAdapter(Protocol):
    """Everything a device has to offer the runner. The simulator and, later, the firmware
    each supply one. ``inject`` and ``read_counters`` are test-only; ``read_counters`` may
    return None for a device that cannot report counters (those checks are then skipped)."""

    def reset(self, config: Mapping[str, Any]) -> None: ...

    def apply_initial(self, initial: Mapping[str, Any]) -> list[bytes]: ...

    def inject(self, fields: Mapping[str, Any]) -> list[bytes]: ...

    def send(self, data: bytes) -> list[bytes]: ...

    def advance_to(self, at_ms: int) -> list[tuple[int, bytes]]: ...

    def set_key(self, on: bool) -> list[bytes]: ...

    def read_status(self) -> StatusBlock: ...

    def read_zone_ids(self) -> list[int]: ...

    def read_counters(self) -> dict[str, int] | None: ...


# ---- reading vectors -----------------------------------------------------------------------


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def load_reasons() -> dict[str, tuple[int, str]]:
    """Reason name -> (permanent code, status) from ``protocol/reason-codes.json``."""
    data = json.loads(REASON_CODES_PATH.read_text(encoding="utf-8"))
    return {entry["name"]: (entry["code"], entry["status"]) for entry in data["reasons"]}


def load_vector_files(directory: Path = VECTOR_DIR) -> list[tuple[str, dict]]:
    """Every vector of every file in the directory as (file name, vector), file by file."""
    validator = Draft202012Validator(load_schema())
    found = []
    for path in sorted(directory.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        errors = sorted(validator.iter_errors(document), key=lambda e: list(e.path))
        if errors:
            raise InvalidVector(f"{path.name}: {errors[0].message} at {list(errors[0].path)}")
        found += [(path.name, vector) for vector in document["vectors"]]
    return found


def plan_steps(vector: Mapping[str, Any]) -> list[dict]:
    """The vector's steps plus its scheduled heartbeats, in the order they are performed.

    Explicit steps must already be in time order. At the same instant a scheduled
    heartbeat goes first, then the explicit steps in the order written.
    """
    explicit = list(vector["steps"])
    for earlier, later in zip(explicit, explicit[1:], strict=False):
        if later["at_ms"] < earlier["at_ms"]:
            raise InvalidVector(
                f"{vector['id']}: steps are not in time order "
                f"({later['at_ms']} ms comes after {earlier['at_ms']} ms)"
            )
    default_every = vector.get("config", {}).get("heartbeat_interval_ms", 200)
    generated = []
    for schedule in vector.get("heartbeats", []):
        every = schedule.get("every_ms", default_every)
        for at_ms in range(schedule["from_ms"], schedule["until_ms"] + 1, every):
            generated.append(
                {
                    "at_ms": at_ms,
                    "action": "send",
                    "generated": True,
                    "message": {
                        "type": "HEARTBEAT",
                        "fields": {"person_clear": schedule.get("person_clear", True)},
                    },
                }
            )
    ordered = [(step["at_ms"], 0, index, step) for index, step in enumerate(generated)]
    ordered += [(step["at_ms"], 1, index, step) for index, step in enumerate(explicit)]
    return [step for *_, step in sorted(ordered, key=lambda item: item[:3])]


# ---- results ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Failure:
    """The first thing that did not match."""

    vector_id: str
    at_ms: int | None
    step: str
    message: str


@dataclass(frozen=True)
class VectorResult:
    vector_id: str
    failure: Failure | None = None
    unchecked: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.failure is None


def format_failure(result: VectorResult) -> str:
    failure = result.failure
    if failure is None:
        return f"{result.vector_id}: passed"
    where = "at the end" if failure.at_ms is None else f"at {failure.at_ms} ms"
    return f"vector {failure.vector_id} failed {where} ({failure.step}): {failure.message}"


class _Mismatch(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class _Event:
    time_ms: int
    event: int
    cause: int
    trigger_ms: int | None


@dataclass(frozen=True)
class _Out:
    frame: Frame
    message: Any


# ---- one run -----------------------------------------------------------------------------------


class _Run:
    def __init__(
        self, vector: Mapping[str, Any], adapter: DeviceAdapter, reasons: Mapping[str, tuple]
    ) -> None:
        self.vector = vector
        self.adapter = adapter
        self.reasons = reasons
        self.events: list[_Event] = []
        self.notices = 0
        self.last_seq = 0
        self.trigger_ms: int | None = None
        self.now_ms = 0
        self.unchecked: list[str] = []
        self.step_text = "start"

    # ---- name resolution ----------------

    def reason_code(self, name: str) -> int:
        entry = self.reasons.get(name)
        if entry is None:
            raise InvalidVector(f"{self.vector['id']}: unknown denial reason {name}")
        code, status = entry
        if status != "active":
            raise InvalidVector(f"{self.vector['id']}: {name} is retired and cannot be expected")
        return code

    def result_code(self, result: str, name: str) -> int:
        if result == "DENIED":
            return self.reason_code(name)
        if result == "REJECTED":
            try:
                return int(RejectCode[name])
            except KeyError:
                raise InvalidVector(f"{self.vector['id']}: unknown reject code {name}") from None
        raise InvalidVector(f"{self.vector['id']}: a {result} reply has no code to name")

    def check_names(self) -> None:
        """Resolve every name in the vector now, so a bad vector is refused before it runs."""
        for step in self.vector["steps"]:
            self._check_expect_reply_names(step.get("expect_reply"))
            self._check_event_names(step.get("expect"))
        self._check_event_names(self.vector.get("expect"))
        for step in self.vector["steps"]:
            if step["action"] == "send":
                self.build_message(step["message"], 0)  # unknown fields, sizes

    def _check_expect_reply_names(self, expected: Any) -> None:
        if isinstance(expected, dict) and "code" in expected:
            self.result_code(expected["result"], expected["code"])

    def _check_event_names(self, expectation: Any) -> None:
        for item in (expectation or {}).get("events", []):
            if "cause" in item:
                self.reason_code(item["cause"])

    # ---- building what is sent ----------------

    def next_seq(self) -> int:
        self.last_seq = (self.last_seq + 1) % _SEQ_MODULUS
        return self.last_seq

    def build_message(self, spec: Mapping[str, Any], at_ms: int):
        cls = _MESSAGE_CLASSES[spec["type"]]
        supplied = dict(spec.get("fields", {}))
        names = [field.name for field in dataclasses.fields(cls)]
        values: dict[str, Any] = {}
        for name in names:
            for key in (name, f"{name}_cdeg"):
                if key in supplied:
                    values[name] = supplied.pop(key)
        if cls is ZoneCommit and "crc32_of" in supplied:
            zone = supplied.pop("crc32_of")
            values["crc32"] = zone_crc32(zone["zone_id"], [tuple(p) for p in zone["points"]])
        if supplied:
            raise InvalidVector(
                f"{self.vector['id']}: {spec['type']} has no field(s) {sorted(supplied)}"
            )
        if cls is FireRequest:
            values.setdefault("issued_at_ms", at_ms)
            values.setdefault("ttl_ms", _DEFAULT_TTL_MS)
            values.setdefault("intended_pan", 0)
            values.setdefault("intended_tilt", 0)
            values.setdefault("duration_ms", _DEFAULT_DURATION_MS)
        elif cls is Heartbeat:
            values.setdefault("person_clear", True)
            values.setdefault("laptop_ts_ms", 0)
        missing = [name for name in names if name not in values]
        if missing:
            raise InvalidVector(f"{self.vector['id']}: {spec['type']} needs field(s) {missing}")
        message = cls(**values)
        try:
            message.encode_payload()
        except ValueError as error:
            raise InvalidVector(f"{self.vector['id']}: {error}") from None
        return message

    # ---- receiving ----------------

    def take(self, wire: bytes, time_ms: int) -> _Out:
        frame = parse_frame(wire[:-1])
        if isinstance(frame, FrameError):
            raise _Mismatch(f"the device sent a bad frame ({frame.value}): {wire.hex(' ')}")
        message = decode_device_message(frame.type, frame.payload)
        if isinstance(message, RejectCode):
            raise _Mismatch(f"the device sent a message that cannot be read ({message.name})")
        if isinstance(message, Reply) and message.result is Result.DENIED:
            if message.code == RETIRED_CODE:
                raise _Mismatch("a DENIED reply carries code 13, which is retired")
        if isinstance(message, EffectorEventMessage):
            if message.event is EffectorEvent.CUT and message.cause == RETIRED_CODE:
                raise _Mismatch("an effector cut carries cause 13, which is retired")
            self.events.append(_Event(time_ms, int(message.event), message.cause, self.trigger_ms))
        elif isinstance(message, RejectNotice):
            self.notices += 1
        return _Out(frame, message)

    def take_all(self, wires: list[bytes], time_ms: int) -> list[_Out]:
        return [self.take(wire, time_ms) for wire in wires]

    # ---- comparing ----------------

    def check_reply(self, expected: Any, outs: list[_Out], seq: int) -> None:
        replies = [o for o in outs if isinstance(o.message, (Reply, GenericReject))]
        if expected == "none":
            if replies:
                raise _Mismatch(f"expected no reply, got {self.describe(replies[0])}")
            return
        if not replies:
            raise _Mismatch(f"no reply came back (expected {expected['result']})")
        if len(replies) > 1:
            raise _Mismatch(f"{len(replies)} replies came back, expected one")
        reply = replies[0]
        if reply.frame.seq != seq:
            raise _Mismatch(f"reply has sequence {reply.frame.seq}, expected {seq}")
        message = reply.message
        if isinstance(message, GenericReject):
            actual_result, actual_code, data = (
                Result.REJECTED,
                int(RejectCode.UNKNOWN_TYPE),
                {"request_type": message.request_type},
            )
        else:
            actual_result, actual_code = message.result, message.code
            data = self.reply_data(message)
        if actual_result.name != expected["result"]:
            raise _Mismatch(f"expected result {expected['result']}, got {self.describe(reply)}")
        if "code" in expected:
            want = self.result_code(expected["result"], expected["code"])
            if actual_code != want:
                raise _Mismatch(
                    f"expected {expected['result']} {expected['code']} (code {want}), "
                    f"got {self.describe(reply)}"
                )
        for key, want in expected.get("data", {}).items():
            if key not in data:
                raise _Mismatch(f"reply data has no {key}; it has {sorted(data)}")
            have = data[key]
            if key == "fault_mask":
                want = self.fault_bits(want)
            elif key == "zones":
                want = self.zone_entries(want)
            if have != want:
                raise _Mismatch(f"reply data {key}: expected {want}, got {have}")

    def describe(self, out: _Out) -> str:
        message = out.message
        if isinstance(message, GenericReject):
            return "REJECTED UNKNOWN_TYPE (generic)"
        if message.result is Result.DENIED:
            name = f" {self.reason_name(message.code)}"
        elif message.result is Result.REJECTED:
            known = {int(code): code.name for code in RejectCode}
            name = f" {known.get(message.code, 'unknown')}"
        else:
            name = ""
        return f"{message.result.name}{name} (code {message.code})"

    def reason_name(self, code: int) -> str:
        for name, (number, _status) in self.reasons.items():
            if number == code:
                return name
        return f"unknown code {code}"

    def reply_data(self, reply: Reply) -> dict[str, Any]:
        try:
            if reply.result is Result.GRANTED:
                return {"granted_on_ms": unpack_granted_on_ms(reply.data)}
            if reply.result is Result.DENIED:
                return {"fault_mask": unpack_fault_mask(reply.data)}
            if reply.result is not Result.OK:
                return {}
            kind = reply.request_type
            if kind in (0x02, 0x03):
                pan, tilt = unpack_angles(reply.data)
                return {"pan": pan, "tilt": tilt}
            if kind == 0x04:
                device_ms, state = unpack_heartbeat_data(reply.data)
                return {"device_ms": device_ms, "state": state.name}
            if kind == 0x14:
                return {
                    "count": len(unpack_zone_list(reply.data)),
                    "zones": [dataclasses.asdict(e) for e in unpack_zone_list(reply.data)],
                }
            if kind == 0x30:
                status = StatusBlock.decode_payload(reply.data)
                if isinstance(status, RejectCode):
                    return {}
                return {**dataclasses.asdict(status), "state": status.state.name}
        except ValueError as error:
            raise _Mismatch(f"reply data cannot be read: {error}") from None
        return {}

    @staticmethod
    def fault_bits(value: Any) -> int:
        if isinstance(value, int):
            return value
        mask = 0
        for name in value:
            mask |= int(Fault[name])
        return mask

    @staticmethod
    def zone_entries(zones: list[dict]) -> list[dict]:
        entries = []
        for zone in zones:
            points = [tuple(p) for p in zone["points"]]
            entries.append(
                {
                    "zone_id": zone["zone_id"],
                    "point_count": len(points),
                    "crc32": zone_crc32(zone["zone_id"], points),
                }
            )
        return entries

    def check_expectation(self, expect: Mapping[str, Any]) -> None:
        status = self.adapter.read_status()
        actual = {
            "state": status.state.name,
            "pan_cdeg": status.pan,
            "tilt_cdeg": status.tilt,
            "armed": status.armed,
            "homed": status.homed,
            "key_on": status.key_on,
            "effector_on": status.effector_on,
            "zone_store_valid": status.zone_store_valid,
        }
        for key, want in expect.items():
            if key in actual:
                if actual[key] != want:
                    raise _Mismatch(f"{key}: expected {want}, got {actual[key]}")
            elif key == "fault_mask":
                want_bits = self.fault_bits(want)
                if status.fault_mask != want_bits:
                    raise _Mismatch(
                        f"fault_mask: expected {want_bits:#06x} ({want}), "
                        f"got {status.fault_mask:#06x}"
                    )
            elif key == "fault_mask_lacks":
                for name in want:
                    if status.fault_mask & int(Fault[name]):
                        raise _Mismatch(f"fault bit {name} is set (mask {status.fault_mask:#06x})")
            elif key == "zones":
                have = sorted(self.adapter.read_zone_ids())
                if have != sorted(want):
                    raise _Mismatch(f"zones: expected {sorted(want)}, got {have}")
            elif key == "events":
                self.check_events(want)
            elif key == "counters":
                self.check_counters(want)

    def check_events(self, wanted: list[dict]) -> None:
        if len(self.events) != len(wanted):
            raise _Mismatch(
                f"events: expected {len(wanted)} effector event(s) {self.event_text(wanted)}, "
                f"got {len(self.events)} {self.seen_text()}"
            )
        for index, (want, have) in enumerate(zip(wanted, self.events, strict=True)):
            if have.event != int(_EVENT_NAMES[want["event"]]):
                raise _Mismatch(
                    f"event {index + 1}: expected {want['event']}, got "
                    f"{EffectorEvent(have.event).name} at {have.time_ms} ms"
                )
            if "cause" in want and have.cause != self.reason_code(want["cause"]):
                raise _Mismatch(
                    f"event {index + 1} ({want['event']}): expected cause {want['cause']}, "
                    f"got code {have.cause} ({self.reason_name(have.cause)})"
                )
            if "at_ms" in want and have.time_ms != want["at_ms"]:
                raise _Mismatch(
                    f"event {index + 1} ({want['event']}): expected at {want['at_ms']} ms, "
                    f"happened at {have.time_ms} ms"
                )
            if "within_ms" in want:
                if have.trigger_ms is None:
                    raise _Mismatch(f"event {index + 1}: within_ms needs an earlier explicit step")
                late = have.time_ms - have.trigger_ms
                if late > want["within_ms"]:
                    raise _Mismatch(
                        f"event {index + 1} ({want['event']}): expected within "
                        f"{want['within_ms']} ms of the step at {have.trigger_ms} ms, "
                        f"happened {late} ms after it (at {have.time_ms} ms)"
                    )

    @staticmethod
    def event_text(wanted: list[dict]) -> str:
        return (
            "["
            + ", ".join(w["event"] + (f" {w['cause']}" if "cause" in w else "") for w in wanted)
            + "]"
        )

    def seen_text(self) -> str:
        names = [EffectorEvent(e.event).name.lower() + f"@{e.time_ms}" for e in self.events]
        return "[" + ", ".join(names) + "]"

    def check_counters(self, wanted: Mapping[str, int]) -> None:
        reported = self.adapter.read_counters()
        for name, want in wanted.items():
            if name == "wire.reject_notice":
                have = self.notices
            elif reported is None:
                self.unchecked.append(name)
                continue
            else:
                have = reported.get(name, 0)
            if have != want:
                raise _Mismatch(f"counter {name}: expected {want}, got {have}")

    # ---- performing steps ----------------

    def perform(self, step: Mapping[str, Any]) -> None:
        action = step["action"]
        at_ms = step["at_ms"]
        if action in _TRIGGER_ACTIONS and not step.get("generated"):
            self.trigger_ms = at_ms
        if action == "send":
            spec = step["message"]
            message = self.build_message(spec, at_ms)
            seq = spec["seq"] if "seq" in spec else self.next_seq()
            self.last_seq = seq
            outs = self.take_all(self.adapter.send(host_frame(message, seq)), at_ms)
            if "expect_reply" in step:
                self.check_reply(step["expect_reply"], outs, seq)
        elif action == "send_raw":
            wire = bytes.fromhex(step["hex"])
            parsed = parse_frame(wire[:-1]) if wire.endswith(b"\x00") else None
            outs = self.take_all(self.adapter.send(wire), at_ms)
            if isinstance(parsed, Frame):
                self.last_seq = parsed.seq
            if "expect_reply" in step:
                seq = parsed.seq if isinstance(parsed, Frame) else -1
                self.check_reply(step["expect_reply"], outs, seq)
        elif action == "set_key":
            self.take_all(self.adapter.set_key(step["on"]), at_ms)
        elif action == "inject":
            self.take_all(self.adapter.inject(self.convert_initial(step["fields"])), at_ms)
        elif action == "expect":
            self.check_expectation(step["expect"])

    def convert_initial(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        converted = dict(fields)
        if "fault_mask" in converted:
            converted["fault_mask"] = self.fault_bits(converted["fault_mask"])
        return converted

    def run(self) -> None:
        vector = self.vector
        self.check_names()
        plan = plan_steps(vector)
        self.adapter.reset(vector.get("config", {}))
        self.step_text = "starting situation"
        self.take_all(
            self.adapter.apply_initial(self.convert_initial(vector.get("initial", {}))), 0
        )
        for number, step in enumerate(plan, start=1):
            self.now_ms = step["at_ms"]
            self.step_text = f"step {number}: {self.step_name(step)}"
            for time_ms, wire in self.adapter.advance_to(step["at_ms"]):
                self.take(wire, time_ms)
            self.perform(step)
        self.step_text = "final expectation"
        if "expect" in vector:
            self.now_ms = None
            self.check_expectation(vector["expect"])

    @staticmethod
    def step_name(step: Mapping[str, Any]) -> str:
        if step["action"] == "send":
            return f"send {step['message']['type']}"
        return step["action"]


def run_vector(
    vector: Mapping[str, Any],
    adapter: DeviceAdapter,
    reasons: Mapping[str, tuple] | None = None,
) -> VectorResult:
    """Play one vector against ``adapter``. Raises ``InvalidVector`` if the vector is wrong;
    returns a result whose ``failure`` is the first mismatch if the device is wrong."""
    document = {"protocol_version": "0.1", "vectors": [vector]}
    errors = sorted(Draft202012Validator(load_schema()).iter_errors(document), key=str)
    if errors:
        raise InvalidVector(f"{vector.get('id', '?')}: {errors[0].message}")
    run = _Run(vector, adapter, reasons if reasons is not None else load_reasons())
    try:
        run.run()
    except _Mismatch as mismatch:
        at_ms = None if run.step_text == "final expectation" else run.now_ms
        failure = Failure(vector["id"], at_ms, run.step_text, mismatch.message)
        return VectorResult(vector["id"], failure, tuple(run.unchecked))
    return VectorResult(vector["id"], None, tuple(run.unchecked))
