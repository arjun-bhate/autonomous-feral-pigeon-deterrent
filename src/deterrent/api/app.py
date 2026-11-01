"""The web application: FastAPI app factory, start-up and shut-down, error shape.

``create_app()`` builds the application. FastAPI is the Python web framework that
turns functions into URLs. This module:

* starts the platform (store, logger, ...) when the server starts and stops it when the
  server stops (the *lifespan*);
* serves the dashboard pages and the files in ``frontend/`` under ``/static``;
* makes **every** error look the same: ``{"error": {"code": "...", "message": "..."}}``;
* only answers requests addressed to ``127.0.0.1`` or ``localhost`` (the server also
  binds only to ``127.0.0.1``), so nothing outside this computer can reach it and a web
  page on another site cannot talk to it through a trick of host names.

Start it with::

    uv run uvicorn deterrent.api.app:create_app --factory --host 127.0.0.1 --port 8000
"""

import mimetypes
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from deterrent.api import routes_read
from deterrent.composition import Platform
from deterrent.config import Settings, load_settings

HOST = "127.0.0.1"
PORT = 8000
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost"})
FRONTEND_DIR = Path(__file__).resolve().parents[3] / "frontend"

# Windows can map .js to text/plain from its registry; browsers refuse such scripts.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")

_STATUS_CODES = {
    400: "BAD_REQUEST",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    422: "INVALID_REQUEST",
}


class ApiError(Exception):
    """Raise this from a route to return the standard error shape."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


class LoopbackHostMiddleware:
    """Refuses any request whose Host header is not this computer's loopback name."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            host = ""
            for name, value in scope["headers"]:
                if name == b"host":
                    host = value.decode("latin-1").rsplit(":", 1)[0].lower()
            if host not in ALLOWED_HOSTS:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    response = error_response(
                        400, "INVALID_HOST", "This server only answers on 127.0.0.1 or localhost."
                    )
                    await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(
    settings: Settings | None = None,
    *,
    platform: Platform | None = None,
    frontend_dir: Path | None = None,
) -> FastAPI:
    settings = settings if settings is not None else load_settings()
    platform = platform if platform is not None else Platform(settings)
    frontend_dir = Path(frontend_dir) if frontend_dir is not None else FRONTEND_DIR

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await platform.start()
        try:
            yield
        finally:
            await platform.stop()

    app = FastAPI(title="Feral Pigeon Deterrent dashboard", lifespan=lifespan)
    app.state.platform = platform
    app.state.settings = settings
    app.state.frontend_dir = frontend_dir
    app.add_middleware(LoopbackHostMiddleware)

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, error: ApiError) -> JSONResponse:
        return error_response(error.status, error.code, error.message)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, error: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODES.get(error.status_code, f"HTTP_{error.status_code}")
        return error_response(error.status_code, code, str(error.detail))

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, error: RequestValidationError) -> JSONResponse:
        first = error.errors()[0] if error.errors() else {}
        where = ".".join(str(part) for part in first.get("loc", ()))
        return error_response(422, "INVALID_REQUEST", f"{where}: {first.get('msg', 'invalid')}")

    app.include_router(routes_read.router)
    app.mount("/static", StaticFiles(directory=frontend_dir, check_dir=False), name="static")
    return app


def main() -> None:
    """Run the server on the loopback address only."""
    uvicorn.run("deterrent.api.app:create_app", factory=True, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
