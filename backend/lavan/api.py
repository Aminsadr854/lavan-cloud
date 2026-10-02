import asyncio
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from lavan.config import Settings

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


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        Settings()  # Validate required configuration at startup.
        app.state.probe_lock = asyncio.Lock()
        yield

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

    return app


app = create_app()
