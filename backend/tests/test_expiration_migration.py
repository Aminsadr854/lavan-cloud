"""Legacy deadline/snapshot preservation and disposable-only downgrade checks."""

from uuid import uuid4

import pytest
from alembic import command
from conftest import migration
from sqlalchemy import MetaData, Table, inspect, select, text
from sqlalchemy.orm import Session

from lavan.jobs import discover_due
from lavan.models import Artifact, ExecutionJob, Project, Run, RunIdempotency, User
from lavan.runs import RunResponse, request_fingerprint


def test_upgrade_preserves_legacy_rows_and_snapshot_bytes(migrated):
    engine, name = migrated
    migration(engine, name, command.downgrade, "0007_run_cancellation")
    old_runs = Table("runs", MetaData(), autoload_with=engine)
    expected = {}
    with Session(engine) as session, session.begin():
        user = User()
        session.add(user)
        session.flush()
        project = Project(owner_id=user.id, name="Expiration migration")
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
        for status in ["queued", "completed", "failed", "stopped"]:
            cancelled = (
                session.scalar(text("SELECT clock_timestamp()"))
                if status == "stopped"
                else None
            )
            row = (
                session.execute(
                    old_runs.insert()
                    .values(
                        id=uuid4(),
                        project_id=project.id,
                        artifact_id=artifact.id,
                        status=status,
                        runtime_template="python-fastapi-v1",
                        cancellation_requested_at=cancelled,
                    )
                    .returning(old_runs)
                )
                .mappings()
                .one()
            )
            snapshot = RunResponse.model_validate(
                {**row, "status": "queued"}
            ).model_dump(mode="json")
            session.add(
                RunIdempotency(
                    user_id=user.id,
                    operation="run.create",
                    key=uuid4().hex,
                    fingerprint=request_fingerprint(
                        project.id, artifact.id, "python-fastapi-v1"
                    ),
                    run_id=row["id"],
                    creation_response=snapshot,
                )
            )
            if status != "queued":
                session.add(ExecutionJob(run_id=row["id"], state=status))
            expected[row["id"]] = (status, cancelled)
    with engine.connect() as connection:
        before = dict(
            connection.execute(
                text("SELECT run_id, creation_response::text FROM run_idempotency")
            ).all()
        )
    for _ in range(2):
        migration(engine, name, command.upgrade, "head")
        assert discover_due(engine) == 0
        with Session(engine) as session:
            for run_id, (status, cancelled) in expected.items():
                run = session.get(Run, run_id)
                assert run.status == status
                assert run.cancellation_requested_at == cancelled
                assert run.expires_at is None and run.expiration_requested_at is None
                job = session.scalar(
                    select(ExecutionJob).where(ExecutionJob.run_id == run_id)
                )
                assert (job is None) if status == "queued" else job.state == status
        with engine.connect() as connection:
            assert (
                dict(
                    connection.execute(
                        text(
                            "SELECT run_id, creation_response::text "
                            "FROM run_idempotency"
                        )
                    ).all()
                )
                == before
            )
        migration(engine, name, command.downgrade, "0007_run_cancellation")
        assert "expires_at" not in {
            c["name"] for c in inspect(engine).get_columns("runs")
        }
    migration(engine, name, command.upgrade, "head")


def test_downgrade_refuses_to_discard_deadlines(migrated):
    engine, name = migrated
    with Session(engine) as session, session.begin():
        user = User()
        session.add(user)
        session.flush()
        project = Project(owner_id=user.id, name="Deadline preservation")
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
        clock = session.scalar(text("SELECT clock_timestamp()"))
        session.add(
            Run(
                project_id=project.id,
                artifact_id=artifact.id,
                runtime_template="python-fastapi-v1",
                created_at=clock,
                expires_at=session.scalar(
                    text("SELECT clock_timestamp()+interval '1 hour'")
                ),
            )
        )
    with pytest.raises(RuntimeError, match="expiration history"):
        migration(engine, name, command.downgrade, "0007_run_cancellation")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0008_run_expiration"
        )
