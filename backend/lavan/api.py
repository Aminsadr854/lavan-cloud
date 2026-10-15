import asyncio
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from lavan.auth import authenticated_user
from lavan.config import Settings
from lavan.db import database_engine
from lavan.models import User

READINESS_TIMEOUT = 2.0
PROBE_COMMAND = (sys.executable, "-m", "lavan.probe")


async def run_probe() -> bool:
    process = None
    try:
        async with asyncio.timeout(READINESS_TIMEOUT):
            process = await asyncio.create_subprocess_exec(
                *PROBE_COMMAND,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            return await process.wait() == 0
    except (TimeoutError, OSError):
        return False
    finally:
        # Cancelling wait() doesn't stop a subprocess. Kill and reap it explicitly,
        # including on client/task cancellation, before permitting another probe.
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


class UserResponse(BaseModel):
    id: UUID
    created_at: datetime


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        Settings()  # Validate required configuration at startup.
        app.state.probe_lock = asyncio.Lock()
        app.state.auth_engine = database_engine()
        try:
            yield
        finally:
            await run_in_threadpool(app.state.auth_engine.dispose)

    app = FastAPI(lifespan=lifespan)

    @app.get("/health/live")
    async def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    async def ready():
        unavailable = JSONResponse(content={"status": "unavailable"}, status_code=503)
        # No waiting queue: overlapping checks fail promptly instead of piling up.
        if app.state.probe_lock.locked():
            return unavailable
        async with app.state.probe_lock:
            if not await run_probe():
                return unavailable
        return {"status": "ready"}

    @app.get("/v1/me", response_model=UserResponse)
    def me(user: Annotated[User, Depends(authenticated_user)]):
        return {"id": user.id, "created_at": user.created_at}

    return app


app = create_app()
