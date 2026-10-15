"""Real PostgreSQL tests; only this fixture creates the downgrade target."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from psycopg import sql
from sqlalchemy import create_engine, inspect, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from lavan.config import Settings
from lavan.models import Base, Project, User


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


def test_initial_upgrade_and_idempotence(migrated):
    engine, name = migrated
    with engine.connect() as connection:
        inspector = inspect(connection)
        assert set(inspector.get_table_names()) == {
            "users",
            "projects",
            "alembic_version",
        }
        assert inspector.get_pk_constraint("users")["name"] == "pk_users"
        assert inspector.get_pk_constraint("projects")["name"] == "pk_projects"
        assert inspector.get_indexes("projects")[0]["name"] == "ix_projects_owner_id"
        assert (
            inspector.get_foreign_keys("projects")[0]["options"]["ondelete"]
            == "RESTRICT"
        )
        assert inspector.get_check_constraints("projects")[0]["name"] == (
            "ck_projects_name_nonblank"
        )
        before = inspector.get_columns("projects")
        assert all(not col["nullable"] for col in before)
        assert {col["name"] for col in before} == {
            "id",
            "owner_id",
            "name",
            "created_at",
            "updated_at",
        }
        assert {col["name"] for col in inspector.get_columns("users")} == {
            "id",
            "created_at",
        }
    migration(engine, name, command.upgrade, "head")
    with engine.connect() as connection:
        after = inspect(connection).get_columns("projects")
        assert [(c["name"], str(c["type"]), c["default"]) for c in before] == [
            (c["name"], str(c["type"]), c["default"]) for c in after
        ]
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0001_users_projects"
        )


def test_persist_user_and_project_with_utc_defaults(migrated):
    engine, _ = migrated
    with Session(engine) as session:
        user = User()
        session.add(user)
        session.flush()
        project = Project(owner_id=user.id, name="First project")
        session.add(project)
        session.commit()
        assert isinstance(user.id, UUID) and isinstance(project.id, UUID)
        assert (
            session.scalar(select(Project).where(Project.id == project.id)) is project
        )
        for timestamp in (user.created_at, project.created_at, project.updated_at):
            assert timestamp.utcoffset() == timedelta(0)
        assert project.owner_id == user.id


def test_nonexistent_owner_rejected(migrated):
    engine, _ = migrated
    with Session(engine) as session:
        session.add(Project(owner_id=uuid4(), name="Orphan"))
        with pytest.raises(IntegrityError) as error:
            session.commit()
        assert error.value.orig.sqlstate == "23503"


@pytest.mark.parametrize("name", ["", " ", "\t\n\r ", "x" * 101])
def test_invalid_name_rejected(migrated, name):
    engine, _ = migrated
    with Session(engine) as session:
        user = User()
        session.add(user)
        session.flush()
        session.add(Project(owner_id=user.id, name=name))
        with pytest.raises(DBAPIError) as error:
            session.flush()
        assert error.value.orig.sqlstate == ("22001" if len(name) > 100 else "23514")


def test_duplicate_names_and_maximum_length_allowed(migrated):
    engine, _ = migrated
    with Session(engine) as session:
        user = User()
        session.add(user)
        session.flush()
        projects = [Project(owner_id=user.id, name="x" * 100) for _ in range(2)]
        session.add_all(projects)
        session.commit()
        assert projects[0].id != projects[1].id


def test_owner_deletion_rejected(migrated):
    engine, _ = migrated
    with Session(engine) as session:
        user = User()
        session.add(user)
        session.flush()
        project = Project(owner_id=user.id, name="Retained")
        session.add(project)
        session.commit()
        user_id, project_id = user.id, project.id
        session.delete(user)
        with pytest.raises(IntegrityError) as error:
            session.commit()
        assert error.value.orig.sqlstate == "23503"
        session.rollback()
        assert session.get(User, user_id) is not None
        assert session.get(Project, project_id) is not None


def test_updated_at_on_orm_and_core_writes(migrated):
    engine, _ = migrated
    old = datetime(2000, 1, 1, tzinfo=UTC)
    with Session(engine) as session:
        user = User()
        session.add(user)
        session.flush()
        project = Project(owner_id=user.id, name="Before", updated_at=old)
        session.add(project)
        session.commit()
        created = project.created_at
        project.name = "After"
        session.commit()
        assert project.updated_at > old
        assert project.updated_at.utcoffset() == timedelta(0)
        assert project.created_at == created
        unchanged = project.updated_at
        session.commit()  # No UPDATE emitted: timestamp must stay unchanged.
        assert project.updated_at == unchanged
        session.execute(
            update(Project)
            .where(Project.id == project.id)
            .values(name="Core update", updated_at=old)
        )
        session.commit()
        session.execute(
            update(Project).where(Project.id == project.id).values(name="Core default")
        )
        session.commit()
        assert project.updated_at > old
        assert project.created_at == created


def test_metadata_has_no_pending_changes(migrated):
    engine, _ = migrated
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={
                "compare_type": True,
                "compare_server_default": True,
            },
        )
        assert compare_metadata(context, Base.metadata) == []


def test_downgrade_and_reupgrade(migrated):
    engine, name = migrated
    migration(engine, name, command.downgrade, "base")
    with engine.connect() as connection:
        assert set(inspect(connection).get_table_names()) <= {"alembic_version"}
    migration(engine, name, command.upgrade, "head")
    with engine.connect() as connection:
        assert {"users", "projects"} <= set(inspect(connection).get_table_names())


def test_alembic_uses_settings_and_discovers_models(migrated, monkeypatch):
    engine, name = migrated
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT current_database()")) == name
    monkeypatch.setenv("POSTGRES_DB", name)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    command.check(config)
