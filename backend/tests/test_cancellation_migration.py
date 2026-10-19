"""Cancellation migration compatibility; destructive checks are disposable-only."""

from uuid import uuid4

import pytest
from alembic import command
from conftest import migration
from sqlalchemy import MetaData, Table, inspect, select, text
from sqlalchemy.orm import Session

from lavan.models import Artifact, ExecutionJob, Project, Run, RunIdempotency, User
from lavan.runs import RunResponse, request_fingerprint


def test_upgrade_preserves_terminal_runs_unscheduled_runs_and_creation_snapshots(
    migrated,
):
    engine, name = migrated
    migration(engine, name, command.downgrade, "0006_simulated_lifecycle")
    old_runs = Table("runs", MetaData(), autoload_with=engine)
    snapshots = {}
    with Session(engine) as session, session.begin():
        user = User()
        session.add(user)
        session.flush()
        project = Project(owner_id=user.id, name="Cancellation migration")
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
        for status in ["completed", "failed", "queued"]:
            run_id = uuid4()
            row = (
                session.execute(
                    old_runs.insert()
                    .values(
                        id=run_id,
                        project_id=project.id,
                        artifact_id=artifact.id,
                        status=status,
                        runtime_template="python-fastapi-v1",
                    )
                    .returning(old_runs)
                )
                .mappings()
                .one()
            )
            # Creation was queued even though GET state is now terminal.
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
                    run_id=run_id,
                    creation_response=snapshot,
                )
            )
            snapshots[run_id] = (status, snapshot)
            if status != "queued":
                session.add(ExecutionJob(run_id=run_id, state=status))
    migration(engine, name, command.upgrade, "head")
    with Session(engine) as session:
        for run_id, (status, snapshot) in snapshots.items():
            run = session.get(Run, run_id)
            record = session.scalar(
                select(RunIdempotency).where(RunIdempotency.run_id == run_id)
            )
            assert run.status == status and run.cancellation_requested_at is None
            assert record.creation_response == snapshot
            job = session.scalar(
                select(ExecutionJob).where(ExecutionJob.run_id == run_id)
            )
            assert (job is None) if status == "queued" else (job.state == status)
    migration(engine, name, command.downgrade, "0006_simulated_lifecycle")
    with engine.connect() as c:
        assert "cancellation_requested_at" not in {
            col["name"] for col in inspect(c).get_columns("runs")
        }
        assert c.scalar(text("SELECT count(*) FROM runs")) == 3
    migration(engine, name, command.upgrade, "head")
    with Session(engine) as session:
        for run_id, (_, snapshot) in snapshots.items():
            assert (
                session.scalar(
                    select(RunIdempotency).where(RunIdempotency.run_id == run_id)
                ).creation_response
                == snapshot
            )


def test_empty_database_downgrade_and_reupgrade(migrated):
    engine, name = migrated
    # Full downgrade below 6C requires queued history; remove only disposable data.
    with engine.begin() as c:
        assert (
            name.startswith("lavan_m2_test_")
            and c.scalar(text("SELECT current_database()")) == name
        )
        for table in ["run_idempotency", "execution_jobs", "run_events", "runs"]:
            c.execute(text("DELETE FROM " + table))
    migration(engine, name, command.downgrade, "base")
    migration(engine, name, command.upgrade, "head")


def test_downgrade_refuses_to_discard_cancellation_history(migrated):
    engine, name = migrated
    with Session(engine) as session, session.begin():
        user = User()
        session.add(user)
        session.flush()
        project = Project(owner_id=user.id, name="Downgrade guard")
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
        session.add(
            Run(
                project_id=project.id,
                artifact_id=artifact.id,
                runtime_template="python-fastapi-v1",
                status="stopped",
                cancellation_requested_at=session.scalar(
                    text("SELECT clock_timestamp()")
                ),
            )
        )
    with pytest.raises(RuntimeError, match="cancellation history"):
        migration(engine, name, command.downgrade, "0006_simulated_lifecycle")
    with engine.connect() as c:
        assert (
            c.scalar(text("SELECT version_num FROM alembic_version"))
            == "0007_run_cancellation"
        )
