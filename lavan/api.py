from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from lavan.db import Session

app = FastAPI(title="Lavan backend", version="0.1.0")


@app.get("/health/live")
def live():
    return {"status": "ok"}


@app.get("/health/ready")
def ready():
    try:
        with Session() as db:
            db.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse({"error": {"code": "database_unavailable"}}, status_code=503)
    return {"status": "ready"}
