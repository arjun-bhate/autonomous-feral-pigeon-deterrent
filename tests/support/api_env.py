"""Test helper: the web app wired to a platform on virtual time, with an HTTP client.

Not a test file. ``api_env`` runs the app's own start-up and shut-down (its
"lifespan"), starts the simulator, and hands back an ``Env`` that also carries an
``httpx`` client talking to the app in memory (no network, no real server).
"""

import contextlib
from pathlib import Path

import httpx
from httpx import ASGITransport

from deterrent.api.app import create_app

from .platform_env import Env

BASE_URL = "http://127.0.0.1:8000"


@contextlib.asynccontextmanager
async def api_env(
    tmp_path,
    *,
    simulator: bool = True,
    homed: bool = True,
    dev_tools: bool = True,
    faults: bool = False,
    sensors: bool = False,
    frontend_dir: Path | None = None,
):
    env = Env(tmp_path, faults=faults, dev_tools=dev_tools, sensors=sensors)
    app = create_app(env.platform.settings, platform=env.platform, frontend_dir=frontend_dir)
    async with app.router.lifespan_context(app):
        await env.start(simulator=simulator, homed=homed)
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
            env.client = client
            env.app = app
            yield env
