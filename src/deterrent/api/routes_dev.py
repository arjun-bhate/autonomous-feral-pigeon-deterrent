"""Developer tools: the owner's and the tests' way to drive the simulator.

These routes exist only while ``PIGEON_DEV_TOOLS=1`` (the ``dev_tools`` setting); when
it is off every one of them answers 404, exactly like a URL that does not exist.

None of them can decide, grant or perform a fire. The only fire-related routes
*send a request* through the normal ``DeviceClient`` and return what the **device**
replied. A route's body accepts no extra fields (so there is no parameter such as
"grant" or "force" to send), and this module imports nothing from the simulator.
"""

from dataclasses import dataclass
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from deterrent.api.errors import ApiError
from deterrent.composition import Platform
from deterrent.engagement.simulated import SimulatorNotRunning
from deterrent.link.client import CommandTimeout
from deterrent.protocol.codes import DenyReason, Fault, MsgType, RejectCode, Result
from deterrent.protocol.messages import (
    Arm,
    Disarm,
    FireStop,
    GenericReject,
    Heartbeat,
    Home,
    HostMessage,
    Jog,
    Move,
    Reply,
    StatusBlock,
    StatusRequest,
    ZoneBegin,
    ZoneClear,
    ZoneCommit,
    ZoneDelete,
    ZoneList,
    ZonePoint,
    unpack_angles,
    unpack_fault_mask,
    unpack_granted_on_ms,
    unpack_heartbeat_data,
    unpack_zone_list,
)


def require_dev_tools(request: Request) -> None:
    if not request.app.state.settings.dev_tools:
        raise ApiError(404, "NOT_FOUND", "Not Found")


router = APIRouter(prefix="/api/dev", dependencies=[Depends(require_dev_tools)])


def get_platform(request: Request) -> Platform:
    return request.app.state.platform


def need_simulator(platform: Platform) -> None:
    if not platform.simulator_running:
        raise ApiError(409, "SIMULATOR_NOT_RUNNING", "Start the simulator first.")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KeyBody(_Strict):
    on: bool


class PersonBody(_Strict):
    clear: bool


class CommandBody(_Strict):
    type: str
    fields: dict[str, Any] = Field(default_factory=dict)


class EngagementBody(_Strict):
    fire: bool = True
    duration_ms: int = Field(default=500, ge=0, le=65535)


class SensorBody(_Strict):
    on: bool
    mode: Literal["error", "hang"] = "error"


class StorageBody(_Strict):
    mode: Literal["full", "locked", "ok"]


# ---- simulator and inputs ----


@router.post("/simulator/start")
async def start_simulator(request: Request) -> dict[str, bool]:
    platform = get_platform(request)
    if platform.simulator_running:
        raise ApiError(409, "SIMULATOR_RUNNING", "The simulator is already running.")
    await platform.start_simulator()
    return {"started": True}


@router.post("/simulator/stop")
async def stop_simulator(request: Request) -> dict[str, bool]:
    await get_platform(request).stop_simulator()
    return {"stopped": True}


@router.post("/key")
async def set_key(body: KeyBody, request: Request) -> dict[str, bool]:
    platform = get_platform(request)
    need_simulator(platform)
    platform.set_key(body.on)
    return {"on": body.on}


@router.post("/person")
async def set_person(body: PersonBody, request: Request) -> dict[str, bool]:
    get_platform(request).set_person_clear(body.clear)
    return {"clear": body.clear}


@router.post("/zone-store/corrupt")
async def corrupt_zone_store(request: Request) -> dict[str, bool]:
    platform = get_platform(request)
    need_simulator(platform)
    try:
        platform.corrupt_zone_store()
    except ValueError as error:
        raise ApiError(
            409, "NO_ZONE_STORED", "Store a zone first; there is nothing to corrupt."
        ) from error
    return {"corrupted": True}


@router.post("/storage/fail")
async def storage_fail(body: StorageBody, request: Request) -> dict[str, str]:
    get_platform(request).set_storage_mode(body.mode)
    return {"mode": body.mode}


@router.post("/sensors/{name}/fail")
async def sensor_fail(name: str, body: SensorBody, request: Request) -> dict[str, Any]:
    """Make one health sensor unreadable (it errors or hangs), or make it work again."""
    platform = get_platform(request)
    try:
        platform.set_sensor_failure(name, body.on, body.mode)
    except KeyError as error:
        raise ApiError(404, "SENSOR_NOT_FOUND", f"There is no sensor called {name!r}.") from error
    return {"name": name, "failed": body.on, "mode": body.mode}


# ---- engagement ----


@router.post("/engagement")
async def engagement(body: EngagementBody, request: Request) -> dict[str, str]:
    platform = get_platform(request)
    try:
        trace_id = await platform.engagement.run(fire=body.fire, duration_ms=body.duration_ms)
    except SimulatorNotRunning as error:
        raise ApiError(409, "SIMULATOR_NOT_RUNNING", "Start the simulator first.") from error
    return {"trace_id": trace_id}


# ---- command ----

_U8 = (0, 255)
_U16 = (0, 65535)
_I16 = (-32768, 32767)
_U32 = (0, 2**32 - 1)


@dataclass(frozen=True)
class FieldSpec:
    attribute: str
    wire_range: tuple[int, int]
    required: bool = True
    default: int = 0


@dataclass(frozen=True)
class CommandSpec:
    message: type
    fields: dict[str, FieldSpec]


COMMANDS: dict[str, CommandSpec] = {
    "HOME": CommandSpec(Home, {}),
    "MOVE": CommandSpec(
        Move, {"pan_cdeg": FieldSpec("pan", _I16), "tilt_cdeg": FieldSpec("tilt", _I16)}
    ),
    "JOG": CommandSpec(
        Jog, {"dpan_cdeg": FieldSpec("dpan", _I16), "dtilt_cdeg": FieldSpec("dtilt", _I16)}
    ),
    "HEARTBEAT": CommandSpec(
        Heartbeat,
        {
            "person_clear": FieldSpec("person_clear", _U8),
            "laptop_ts_ms": FieldSpec("laptop_ts_ms", _U32, required=False),
        },
    ),
    "ARM": CommandSpec(Arm, {}),
    "DISARM": CommandSpec(Disarm, {}),
    "ZONE_BEGIN": CommandSpec(
        ZoneBegin,
        {"zone_id": FieldSpec("zone_id", _U8), "point_count": FieldSpec("point_count", _U8)},
    ),
    "ZONE_POINT": CommandSpec(
        ZonePoint,
        {
            "index": FieldSpec("index", _U8),
            "pan_cdeg": FieldSpec("pan", _I16),
            "tilt_cdeg": FieldSpec("tilt", _I16),
        },
    ),
    "ZONE_COMMIT": CommandSpec(ZoneCommit, {"crc32": FieldSpec("crc32", _U32)}),
    "ZONE_DELETE": CommandSpec(ZoneDelete, {"zone_id": FieldSpec("zone_id", _U8)}),
    "ZONE_LIST": CommandSpec(ZoneList, {}),
    "ZONE_CLEAR": CommandSpec(ZoneClear, {}),
    "FIRE_STOP": CommandSpec(FireStop, {}),
    "STATUS_REQUEST": CommandSpec(StatusRequest, {}),
}

# FIRE_REQUEST is sent through client.fire_request, which stamps the device time itself.
FIRE_FIELDS = {
    "duration_ms": FieldSpec("duration_ms", _U16),
    "ttl_ms": FieldSpec("ttl_ms", _U16, required=False, default=-1),
    "intended_pan_cdeg": FieldSpec("intended_pan", _I16, required=False),
    "intended_tilt_cdeg": FieldSpec("intended_tilt", _I16, required=False),
}


def read_fields(specs: dict[str, FieldSpec], given: dict[str, Any]) -> dict[str, int]:
    """Check the given fields against the allowed names, types and wire ranges."""
    for name in given:
        if name not in specs:
            raise ApiError(400, "UNKNOWN_FIELD", f"Unknown field {name!r}.")
    values: dict[str, int] = {}
    for name, spec in specs.items():
        if name not in given:
            if spec.required:
                raise ApiError(400, "MISSING_FIELD", f"Field {name!r} is required.")
            values[spec.attribute] = spec.default
            continue
        value = given[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ApiError(400, "INVALID_FIELD", f"Field {name!r} must be a whole number.")
        low, high = spec.wire_range
        if not low <= value <= high:
            raise ApiError(
                400,
                "FIELD_OUT_OF_WIRE_RANGE",
                f"Field {name!r} must be between {low} and {high} to fit the message.",
            )
        values[spec.attribute] = value
    return values


def _faults(mask: int) -> list[str]:
    return [fault.name for fault in Fault if mask & fault]


def _name(enum_type, code: int) -> str:
    try:
        return enum_type(code).name
    except ValueError:
        return f"CODE_{code}"


def reply_to_json(request_name: str, reply: Reply | GenericReject) -> dict[str, Any]:
    """The device's reply as JSON: names only, never numeric codes."""
    if isinstance(reply, GenericReject):
        return {"type": request_name, "result": "REJECTED", "reject": RejectCode.UNKNOWN_TYPE.name}
    body: dict[str, Any] = {"type": request_name, "result": reply.result.name}
    if reply.result is Result.REJECTED:
        body["reject"] = _name(RejectCode, reply.code)
    elif reply.result is Result.DENIED:
        body["denial_reason"] = _name(DenyReason, reply.code)
        body["faults"] = _faults(unpack_fault_mask(reply.data))
    elif reply.result is Result.GRANTED:
        body["granted_on_ms"] = unpack_granted_on_ms(reply.data)
    else:
        data = _reply_data(reply)
        if data is not None:
            body["data"] = data
    return body


def _reply_data(reply: Reply) -> dict[str, Any] | None:
    kind = reply.request_type
    if kind in (MsgType.MOVE, MsgType.JOG):
        pan, tilt = unpack_angles(reply.data)
        return {"pan_cdeg": pan, "tilt_cdeg": tilt}
    if kind == MsgType.HEARTBEAT:
        device_ms, state = unpack_heartbeat_data(reply.data)
        return {"device_ms": device_ms, "state": state.name}
    if kind == MsgType.STATUS_REQUEST:
        block = StatusBlock.decode_payload(reply.data)
        if isinstance(block, StatusBlock):
            return {
                "state": block.state.name,
                "pan_cdeg": block.pan,
                "tilt_cdeg": block.tilt,
                "key_on": block.key_on,
                "armed": block.armed,
                "homed": block.homed,
                "zone_store_valid": block.zone_store_valid,
                "effector_on": block.effector_on,
                "faults": _faults(block.fault_mask),
                "zone_count": block.zone_count,
            }
    if kind == MsgType.ZONE_LIST:
        return {
            "zones": [
                {"zone_id": z.zone_id, "point_count": z.point_count, "crc32": z.crc32}
                for z in unpack_zone_list(reply.data)
            ]
        }
    return None


@router.post("/command")
async def send_command(body: CommandBody, request: Request) -> dict[str, Any]:
    """Send one protocol command through the normal client and return the device's reply."""
    platform = get_platform(request)
    name = body.type.upper()
    if name != "FIRE_REQUEST" and name not in COMMANDS:
        raise ApiError(400, "UNKNOWN_COMMAND", f"Unknown command type {body.type!r}.")
    need_simulator(platform)
    client = platform.client
    try:
        if name == "FIRE_REQUEST":
            values = read_fields(FIRE_FIELDS, body.fields)
            ttl = None if values["ttl_ms"] < 0 else values["ttl_ms"]
            reply = await client.fire_request(
                values["intended_pan"], values["intended_tilt"], values["duration_ms"], ttl
            )
        else:
            spec = COMMANDS[name]
            message: HostMessage = spec.message(**read_fields(spec.fields, body.fields))
            reply = await client.request(message)
    except CommandTimeout as error:
        raise ApiError(504, "DEVICE_TIMEOUT", "The device did not reply in time.") from error
    except ValueError as error:  # a value that does not fit its field on the wire
        raise ApiError(400, "FIELD_OUT_OF_WIRE_RANGE", str(error)) from error
    return reply_to_json(name, reply)
