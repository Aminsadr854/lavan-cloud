"""Disposable PostgreSQL fixtures; never downgrade the development database."""

import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import NullPool

from lavan.config import Settings


@pytest.fixture(scope="module")
def database():
    if os.environ.get("LAVAN_POSTGRES_TESTS") != "1":
        pytest.skip("Enable real PostgreSQL tests with LAVAN_POSTGRES_TESTS=1")
    settings = Settings()
    name = "lavan_m2_test_" + uuid4().hex
    assert name != settings.postgres_db
    admin = create_engine(
        settings.database_url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
        connect_args={"connect_timeout": 2},
        hide_parameters=True,
    )
    engine = None
    created = False
    try:
        with admin.connect() as connection:
            raw = connection.connection.driver_connection
            raw.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                    sql.Identifier(name)
                )
            )
            created = True
        engine = create_engine(
            settings.database_url.set(database=name),
            poolclass=NullPool,
            connect_args={
                "connect_timeout": 2,
                "options": "-c timezone=UTC -c statement_timeout=10000",
            },
            hide_parameters=True,
        )
        assert engine.url.database == name
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT current_database()")) == name
            assert inspect(connection).get_table_names() == []
        yield engine, name
    finally:
        if engine is not None:
            engine.dispose()
        if created:
            # The exact UUID name was created above, never supplied by the caller.
            assert name.startswith("lavan_m2_test_") and name != settings.postgres_db
            with admin.connect() as connection:
                connection.connection.driver_connection.execute(
                    sql.SQL("DROP DATABASE {}").format(sql.Identifier(name))
                )
        admin.dispose()


def migration(engine, name, operation, revision):
    assert engine.url.database == name and name.startswith("lavan_m2_test_")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT current_database()")) == name
        connection.rollback()
        config.attributes["connection"] = connection
        operation(config, revision)


@pytest.fixture(scope="module")
def migrated(database):
    engine, name = database
    migration(engine, name, command.upgrade, "head")
    return engine, name
