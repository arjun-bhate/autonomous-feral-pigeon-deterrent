"""Read-only endpoints and the page routes.

Nothing here changes the system, and nothing here can grant or perform a fire. The
status endpoints read the platform's in-memory state, so they answer at once even if
the disk is slow or failing.
"""

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from deterrent.composition import Platform

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


@router.get("/api/device/status")
async def device_status(request: Request) -> dict[str, Any]:
    return get_platform(request).device_status()


@router.get("/api/system/status")
async def get_system_status(request: Request) -> dict[str, Any]:
    return system_status(get_platform(request), request.app.state.settings.dev_tools)


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
