from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from lavan.config import Settings


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = Settings()
        # A fresh connection avoids stale pooled sockets after database restarts.
        # libpq bounds connection establishment and socket failure detection.
        engine = create_engine(
            settings.database_url,
            poolclass=NullPool,
            connect_args={
                "connect_timeout": 2,
                "options": "-c statement_timeout=2000",
                "keepalives": 1,
                "keepalives_idle": 2,
                "keepalives_interval": 1,
                "keepalives_count": 2,
                "tcp_user_timeout": 4000,
            },
        )
        app.state.engine = engine
        try:
            yield
        finally:
            engine.dispose()

    app = FastAPI(lifespan=lifespan)

    @app.get("/health/live")
    def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    def ready():
        try:
            with app.state.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return JSONResponse(status_code=503, content={"status": "unavailable"})
        return {"status": "ready"}

    return app


app = create_app()
