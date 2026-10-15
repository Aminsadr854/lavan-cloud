"""Synchronous database access; FastAPI runs its consumers in worker threads."""

from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from lavan.config import Settings


def database_engine():
    return create_engine(
        Settings().database_url,
        poolclass=NullPool,
        connect_args={
            "connect_timeout": 2,
            "options": "-c timezone=UTC -c statement_timeout=2000",
            "tcp_user_timeout": 1500,
        },
        hide_parameters=True,
    )
