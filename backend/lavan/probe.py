"""Disposable database probe; never print connection errors or credentials."""

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from lavan.config import Settings


def check_database() -> None:
    settings = Settings()
    engine = create_engine(
        settings.database_url,
        poolclass=NullPool,
        connect_args={
            "connect_timeout": 2,
            "options": "-c statement_timeout=1000",
            "keepalives": 1,
            "keepalives_idle": 2,
            "keepalives_interval": 1,
            "keepalives_count": 2,
            "tcp_user_timeout": 1500,
        },
    )
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        check_database()
    except Exception:
        raise SystemExit(1) from None
