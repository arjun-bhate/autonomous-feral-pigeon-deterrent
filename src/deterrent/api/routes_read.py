"""Read-only endpoints and the page routes.

Nothing here changes the system, and nothing here can grant or perform a fire. The
status endpoints read the platform's in-memory state, so they answer at once even if
the disk is slow or failing. The history endpoints (logs, and later traces and metrics)
read the database through a read-only connection, on a worker thread, so a slow disk
never blocks the live channel; if the database cannot be read they answer 503.
"""

import asyncio
import sqlite3
from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from deterrent.api.errors import ApiError
from deterrent.composition import Platform
from deterrent.observability import queries
from deterrent.observability.store import LogRecord

router = APIRouter()

PAGES = {
    "/": "index.html",
    "/logs": "logs.html",
    "/traces": "traces.html",
    "/observability": "observability.html",
}


def get_platform(request: Request) -> Platform:
    return request.app.state.platform


def system_status(platform: Platform, dev_tools: bool) -> dict[str, Any]:
    warning = platform.storage_warning
    return {
        "simulator_running": platform.simulator_running,
        "storage_warning": (
            None if warning is None else {"message": warning.message, "since_us": warning.since_us}
        ),
        "dev_tools": dev_tools,
    }


async def read_database[T](platform: Platform, work: Callable[[sqlite3.Connection], T]) -> T:
    """Run a read-only question on a worker thread; an unreadable database is a 503."""

    def run() -> T:
        conn = platform.store.open_read_connection()
        try:
            return work(conn)
        finally:
            conn.close()

    try:
        return await asyncio.to_thread(run)
    except sqlite3.Error as error:
        raise ApiError(
            503, "STORAGE_UNAVAILABLE", "The log database cannot be read right now."
        ) from error


def log_item(record: LogRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "ts_us": record.ts_us,
        "component": record.component,
        "severity": record.severity,
        "message": record.message,
        "trace_id": record.trace_id,
        "span_id": record.span_id,
        "fields": record.fields,
    }


def health_payload(platform: Platform) -> dict[str, Any]:
    return {"readings": platform.health_readings()}


@router.get("/api/health/current")
async def health_current(request: Request) -> dict[str, Any]:
    return health_payload(get_platform(request))


@router.get("/api/device/status")
async def device_status(request: Request) -> dict[str, Any]:
    return get_platform(request).device_status()


@router.get("/api/system/status")
async def get_system_status(request: Request) -> dict[str, Any]:
    return system_status(get_platform(request), request.app.state.settings.dev_tools)


# ---- logs ----

Severity = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


@router.get("/api/logs")
async def get_logs(
    request: Request,
    component: str | None = None,
    severity: Severity | None = None,
    trace_id: str | None = None,
    limit: Annotated[int, Query(ge=1)] = queries.DEFAULT_LOG_LIMIT,
    before_id: int | None = None,
) -> dict[str, Any]:
    """The newest matching entries (oldest first within the page). Filters combine with AND."""
    page = await read_database(
        get_platform(request),
        lambda conn: queries.query_logs(
            conn,
            component=component,
            severity=severity,
            trace_id=trace_id,
            limit=limit,
            before_id=before_id,
        ),
    )
    return {"items": [log_item(record) for record in page.items], "has_more": page.has_more}


@router.get("/api/logs/components")
async def get_log_components(request: Request) -> dict[str, Any]:
    return {"items": await read_database(get_platform(request), queries.list_components)}


# ---- traces ----


def trace_summary(item: queries.TraceSummary) -> dict[str, Any]:
    return {
        "trace_id": item.trace_id,
        "started_us": item.started_us,
        "ended_us": item.ended_us,
        "total_us": item.total_us,
        "outcome": item.outcome,
        "deny_reason": item.deny_reason,
        "simulated": item.simulated,
    }


def span_item(item: queries.SpanItem) -> dict[str, Any]:
    return {
        "span_id": item.span_id,
        "position": item.position,
        "name": item.name,
        "started_us": item.started_us,
        "duration_us": item.duration_us,
        "status": item.status,
        "simulated": item.simulated,
        "attrs": item.attrs,
    }


@router.get("/api/traces")
async def get_traces(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=500)] = queries.DEFAULT_TRACE_LIMIT,
) -> dict[str, Any]:
    """The newest traces first."""
    items = await read_database(
        get_platform(request), lambda conn: queries.list_traces(conn, limit)
    )
    return {"items": [trace_summary(item) for item in items]}


@router.get("/api/traces/{trace_id}")
async def get_trace(trace_id: str, request: Request) -> dict[str, Any]:
    """One trace with its spans in order. An unknown id is 404 TRACE_NOT_FOUND."""
    detail = await read_database(
        get_platform(request), lambda conn: queries.get_trace(conn, trace_id)
    )
    if detail is None:
        raise ApiError(404, "TRACE_NOT_FOUND", f"There is no trace with id {trace_id!r}.")
    return {**trace_summary(detail), "spans": [span_item(s) for s in detail.spans]}


def _page(name: str):
    async def serve(request: Request):
        path = request.app.state.frontend_dir / name
        if not path.is_file():
            return JSONResponse(
                {"error": {"code": "PAGE_NOT_FOUND", "message": f"{name} is not built yet."}},
                status_code=404,
            )
        return FileResponse(path, media_type="text/html")

    return serve


for _url, _file in PAGES.items():
    router.add_api_route(_url, _page(_file), methods=["GET"], include_in_schema=False)
