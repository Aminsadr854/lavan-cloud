"""Only the fixture-created disposable PostgreSQL database is migrated backward."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from conftest import migration
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from lavan.models import Artifact, ExecutionJob, Project, Run, RunIdempotency, User
from lavan.runs import RunResponse, request_fingerprint


def test_upgrade_legacy_queued_rows_backfills_exact_snapshot_without_jobs(migrated):
    engine, name = migrated
    migration(engine, name, command.downgrade, "0005_run_idempotency")
    with Session(engine) as session, session.begin():
        user = User()
        session.add(user)
        session.flush()
        project = Project(owner_id=user.id, name="Legacy snapshot")
        session.add(project)
        session.flush()
        artifact = Artifact(
            project_id=project.id,
            storage_key=uuid4().hex,
            size_bytes=0,
            sha256="0" * 64,
            file_count=1,
            declared_uncompressed_bytes=0,
        )
        session.add(artifact)
        session.flush()
        ids = user.id, project.id, artifact.id
        run_ids = [uuid4(), uuid4()]
        for run_id, micros in zip(run_ids, [0, 123456], strict=True):
            created = datetime(2026, 1, 2, 3, 4, 5, micros, tzinfo=UTC)
            session.execute(
                text(
                    "INSERT INTO runs (id,project_id,artifact_id,status,"
                    "runtime_template,created_at) VALUES (:id,:project,:artifact,"
                    "'queued','python-fastapi-v1',:created)"
                ),
                {
                    "id": run_id,
                    "project": project.id,
                    "artifact": artifact.id,
                    "created": created,
                },
            )
            session.execute(
                text(
                    "INSERT INTO run_idempotency (user_id,operation,key,fingerprint,"
                    "run_id) VALUES (:user,'run.create',:key,:fingerprint,:run)"
                ),
                {
                    "user": user.id,
                    "key": uuid4().hex,
                    "fingerprint": request_fingerprint(
                        project.id, artifact.id, "python-fastapi-v1"
                    ),
                    "run": run_id,
                },
            )
    migration(engine, name, command.upgrade, "head")
    with Session(engine) as session:
        assert session.scalars(select(ExecutionJob)).all() == []
        for run_id in run_ids:
            run = session.get(Run, run_id)
            record = session.scalar(
                select(RunIdempotency).where(RunIdempotency.run_id == run_id)
            )
            assert record.creation_response == RunResponse.model_validate(
                run
            ).model_dump(mode="json")
            assert run.updated_at == run.created_at
        assert session.get(User, ids[0]) is not None
    migration(engine, name, command.downgrade, "0005_run_idempotency")
    with engine.connect() as c:
        assert "execution_jobs" not in inspect(c).get_table_names()
        assert c.scalar(text("SELECT count(*) FROM runs")) == 2
    migration(engine, name, command.upgrade, "head")


def test_empty_database_full_downgrade_and_upgrade(migrated):
    engine, name = migrated
    migration(engine, name, command.downgrade, "base")
    with engine.connect() as c:
        assert inspect(c).get_table_names() == ["alembic_version"]
    migration(engine, name, command.upgrade, "head")


def test_backfill_rejects_nonqueued_assumption_violation(migrated):
    engine, name = migrated
    migration(engine, name, command.downgrade, "0005_run_idempotency")
    # Build a deliberately inconsistent old schema only in the disposable DB.
    with engine.begin() as c:
        assert (
            name.startswith("lavan_m2_test_")
            and c.scalar(text("SELECT current_database()")) == name
        )
        c.execute(text("ALTER TABLE runs DROP CONSTRAINT ck_runs_status_queued"))
        user, project, artifact, run = [uuid4() for _ in range(4)]
        c.execute(text("INSERT INTO users (id) VALUES (:id)"), {"id": user})
        c.execute(
            text(
                "INSERT INTO projects (id,owner_id,name,updated_at) "
                "VALUES (:id,:user,'guard',now())"
            ),
            {"id": project, "user": user},
        )
        c.execute(
            text(
                "INSERT INTO artifacts (id,project_id,storage_key,size_bytes,"
                "sha256,file_count,declared_uncompressed_bytes) "
                "VALUES (:id,:project,:key,0,:sha,1,0)"
            ),
            {"id": artifact, "project": project, "key": uuid4().hex, "sha": "0" * 64},
        )
        c.execute(
            text(
                "INSERT INTO runs (id,project_id,artifact_id,status,"
                "runtime_template) VALUES (:id,:project,:artifact,'running',"
                "'python-fastapi-v1')"
            ),
            {"id": run, "project": project, "artifact": artifact},
        )
    with pytest.raises(RuntimeError, match="all existing runs"):
        migration(engine, name, command.upgrade, "head")
    with engine.begin() as c:
        assert (
            c.scalar(text("SELECT version_num FROM alembic_version"))
            == "0005_run_idempotency"
        )
        c.execute(text("UPDATE runs SET status='queued'"))
        c.execute(
            text(
                "ALTER TABLE runs ADD CONSTRAINT ck_runs_status_queued "
                "CHECK (status='queued')"
            )
        )
    migration(engine, name, command.upgrade, "head")
