"""Real PostgreSQL fencing and actual process-crash tests; no development writes."""

import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from lavan.api import create_app
from lavan.config import Settings
from lavan.jobs import InvalidTransition, LostLease, claim, enqueue, retry, transition
from lavan.models import (
    ApiToken,
    Artifact,
    ExecutionJob,
    Project,
    Run,
    RunEvent,
    RunIdempotency,
    User,
)
from lavan.runner import SimulatedRunner
from lavan.tokens import new_token, token_digest
from lavan.worker import process, tick


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        Event().wait(0.02)
    pytest.fail("Bounded polling deadline exceeded")


@pytest.fixture
def work(migrated):
    engine, name = migrated
    with Session(engine) as session, session.begin():
        assert session.scalar(text("SELECT current_database()")) == name
        assert name.startswith("lavan_m2_test_")
        # Only the fixture-created disposable DB; clear worker inputs between tests.
        for model in (RunIdempotency, RunEvent, ExecutionJob, Run):
            session.execute(delete(model))
        user = User()
        session.add(user)
        session.flush()
        project = Project(owner_id=user.id, name="Worker fixture")
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
        run = Run(
            project_id=project.id,
            artifact_id=artifact.id,
            runtime_template="python-fastapi-v1",
        )
        session.add(run)
        session.flush()
        result = SimpleNamespace(
            engine=engine,
            name=name,
            run=run.id,
            artifact=artifact.id,
            project=project.id,
            user=user.id,
        )
    return result


def settings(**overrides):
    return Settings(
        worker_lease_seconds=0.4,
        worker_poll_seconds=0.02,
        worker_retry_seconds=0.05,
        simulator_startup_seconds=0,
        simulator_duration_seconds=0,
        **overrides,
    )


def schedule(work):
    with Session(work.engine) as session, session.begin():
        return enqueue(session, work.run)


def state(work):
    with Session(work.engine) as session:
        run = session.get(Run, work.run)
        job = session.scalar(
            select(ExecutionJob).where(ExecutionJob.run_id == work.run)
        )
        events = session.scalars(
            select(RunEvent)
            .where(RunEvent.run_id == work.run)
            .order_by(RunEvent.sequence)
        ).all()
        return SimpleNamespace(
            status=run.status,
            error=run.error_code,
            job_state=job.state if job else None,
            attempts=job.attempts if job else None,
            fence=job.fence if job else None,
            lease_until=job.lease_until if job else None,
            anchor=job.accepted_at if job else None,
            events=[(e.id, e.sequence, e.status) for e in events],
        )


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("success", "completed"),
        ("startup_failure", "failed"),
        ("slow_startup", "completed"),
    ],
)
def test_scenarios_forward_events_and_no_transaction_during_runner(
    work, scenario, expected
):
    config = settings(simulator_scenario=scenario, simulator_slow_startup_seconds=0.5)
    schedule(work)
    lease = claim(work.engine, config)

    class CheckingRunner(SimulatedRunner):
        def inspect(self, request, now):
            # Both rows must be unlocked while a runner operation runs.
            with Session(work.engine) as session, session.begin():
                session.execute(text("SET LOCAL lock_timeout = '100ms'"))
                session.scalar(
                    select(ExecutionJob)
                    .where(ExecutionJob.id == lease.job_id)
                    .with_for_update(nowait=True)
                )
                session.scalar(
                    select(Run).where(Run.id == work.run).with_for_update(nowait=True)
                )
            assert request.artifact_id == work.artifact
            assert request.artifact_sha256 == "0" * 64
            assert request.operation_id == f"run:{work.run}:start:v1"
            return super().inspect(request, now)

    process(work.engine, lease, config, Event(), CheckingRunner())
    result = state(work)
    assert result.status == expected and result.job_state == expected
    assert result.attempts == 1
    expected_events = (
        ["preparing", "starting", "failed"]
        if expected == "failed"
        else ["preparing", "starting", "running", "completed"]
    )
    assert [e[2] for e in result.events] == expected_events
    assert [e[1] for e in result.events] == list(range(1, len(expected_events) + 1))
    assert len({e[0] for e in result.events}) == len(expected_events)
    assert result.error == (
        "simulated_startup_failure" if expected == "failed" else None
    )
    assert claim(work.engine, config) is None
    with pytest.raises(LostLease):
        transition(work.engine, lease, "failed")


def test_two_concurrent_claimers_only_one_lease(work):
    schedule(work)
    barrier = Barrier(2, timeout=5)
    config = settings()

    def take(_):
        barrier.wait()
        return claim(work.engine, config)

    with ThreadPoolExecutor(max_workers=2) as executor:
        leases = list(executor.map(take, range(2)))
    assert sum(lease is not None for lease in leases) == 1
    assert state(work).attempts == 1


def test_expired_and_fenced_workers_cannot_mutate_or_append_events(work):
    schedule(work)
    config = settings()
    old = claim(work.engine, config)
    transition(work.engine, old, "preparing")
    before = state(work).events
    with Session(work.engine) as session, session.begin():
        job = session.get(ExecutionJob, old.job_id)
        job.lease_until = session.scalar(select(func.clock_timestamp())) - timedelta(
            seconds=1
        )
    with pytest.raises(LostLease):
        tick(work.engine, old, config, SimulatedRunner())
    new = claim(work.engine, config)
    assert new.fence > old.fence and new.owner != old.owner
    for action in (
        lambda: transition(work.engine, old, "starting"),
        lambda: retry(work.engine, old, config),
        lambda: tick(work.engine, old, config, SimulatedRunner()),
    ):
        with pytest.raises(LostLease):
            action()
    assert state(work).events == before
    with pytest.raises(InvalidTransition):
        transition(work.engine, new, "queued")
    process(work.engine, new, config, Event())
    assert state(work).status == "completed"
    assert state(work).events[:1] == before


def test_retry_exhaustion_and_no_backwards_resume(work):
    schedule(work)
    config = settings()

    class BrokenRunner:
        def start(self, *_):
            raise RuntimeError("internal infrastructure fixture")

        inspect = start

    for attempt in range(1, 4):
        lease = wait_for(lambda: claim(work.engine, config))
        before_attempt = database_clock(work)
        process(work.engine, lease, config, Event(), BrokenRunner())
        result = state(work)
        assert result.attempts == attempt
        assert result.status == ("failed" if attempt == 3 else "starting")
        if attempt < 3:
            assert result.job_state == "pending"
            with Session(work.engine) as session:
                scheduled = session.get(ExecutionJob, lease.job_id).available_at
                assert scheduled >= before_attempt + timedelta(
                    seconds=config.worker_retry_seconds * attempt
                )
    assert result.error == "retries_exhausted" and result.job_state == "failed"
    assert [e[2] for e in result.events] == ["preparing", "starting", "failed"]


def test_crash_exhaustion_is_terminal(work):
    schedule(work)
    config = settings()
    for _ in range(3):
        lease = claim(work.engine, config)
        with Session(work.engine) as session, session.begin():
            session.get(ExecutionJob, lease.job_id).lease_until = session.scalar(
                select(func.clock_timestamp())
            ) - timedelta(seconds=1)
    assert claim(work.engine, config) is None
    assert state(work).status == "failed"
    assert state(work).error == "retries_exhausted"
    assert claim(work.engine, config) is None


def test_old_run_unscheduled_and_explicit_enqueue_idempotent(work):
    assert claim(work.engine, settings()) is None
    first = schedule(work)
    assert first == schedule(work)
    with Session(work.engine) as session:
        assert session.scalar(select(func.count()).select_from(ExecutionJob)) == 1
    with Session(work.engine) as session, session.begin():
        with pytest.raises(LookupError, match="not found"):
            enqueue(session, uuid4())
    lease = claim(work.engine, settings())
    process(work.engine, lease, settings(), Event())
    with Session(work.engine) as session, session.begin():
        with pytest.raises(LookupError, match="queued"):
            enqueue(session, work.run)


def worker_process(work, **environment):
    env = {
        **os.environ,
        "POSTGRES_DB": work.name,
        "WORKER_LEASE_SECONDS": "0.4",
        "WORKER_POLL_SECONDS": "0.02",
        "SIMULATOR_SCENARIO": "slow_startup",
        "SIMULATOR_SLOW_STARTUP_SECONDS": "1.5",
        "SIMULATOR_DURATION_SECONDS": "0.1",
        **environment,
    }
    assert env["POSTGRES_DB"].startswith("lavan_m2_test_")
    return subprocess.Popen(
        [sys.executable, "-m", "lavan.worker", "--once"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_real_worker_sigkill_recovered_by_another_process(work):
    schedule(work)
    first = worker_process(work)
    second = None
    try:
        initial = wait_for(
            lambda: s if (s := state(work)).status == "starting" else None
        )
        first.kill()
        first.wait(timeout=5)
        assert first.returncode < 0
        anchor, original_events = initial.anchor, initial.events
        wait_for(lambda: state(work).lease_until <= database_clock(work))
        second = worker_process(work, SIMULATOR_SCENARIO="startup_failure")
        assert second.wait(timeout=10) == 0
        recovered = state(work)
        assert recovered.status == "completed" and recovered.attempts == 2
        assert recovered.anchor == anchor
        assert recovered.events[:2] == original_events
        assert [e[2] for e in recovered.events] == [
            "preparing",
            "starting",
            "running",
            "completed",
        ]
    finally:
        for proc in (first, second):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


def database_clock(work):
    with Session(work.engine) as session:
        return session.scalar(select(func.clock_timestamp()))


def test_real_worker_renews_slow_lease(work):
    schedule(work)
    worker = worker_process(work)
    try:
        initial = wait_for(
            lambda: s if (s := state(work)).status == "starting" else None
        )
        renewed = wait_for(
            lambda: (
                s
                if (s := state(work)).lease_until
                and s.lease_until > initial.lease_until + timedelta(seconds=0.5)
                else None
            )
        )
        assert renewed.status == "starting" and renewed.attempts == 1
        assert claim(work.engine, settings()) is None
        assert worker.wait(timeout=10) == 0
        assert state(work).status == "completed" and state(work).attempts == 1
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)


def test_graceful_shutdown_leaves_recoverable_lease(work):
    schedule(work)
    worker = worker_process(work)
    try:
        wait_for(lambda: state(work).status == "starting")
        worker.terminate()
        assert worker.wait(timeout=5) == 0
        assert state(work).job_state == "leased"
        wait_for(lambda: state(work).lease_until <= database_clock(work))
        lease = claim(work.engine, settings())
        assert lease is not None
        process(work.engine, lease, settings(), Event())
        assert state(work).status == "completed"
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)


def test_http_snapshot_after_worker_completion_and_atomic_job(work, monkeypatch):
    monkeypatch.setenv("POSTGRES_DB", work.name)
    raw = new_token()
    with Session(work.engine) as session, session.begin():
        session.add(ApiToken(user_id=work.user, digest=token_digest(raw)))
    headers = {"Authorization": "Bearer " + raw, "Idempotency-Key": uuid4().hex}
    payload = {
        "artifact_id": str(work.artifact),
        "runtime_template": "python-fastapi-v1",
    }
    with TestClient(create_app()) as client:
        first = client.post(
            f"/v1/projects/{work.project}/runs", json=payload, headers=headers
        )
        assert first.status_code == 201 and first.json()["status"] == "queued"
        lease = claim(work.engine, settings())
        process(work.engine, lease, settings(), Event())
        replay = client.post(
            f"/v1/projects/{work.project}/runs", json=payload, headers=headers
        )
        assert replay.status_code == 201 and replay.json() == first.json()
        assert replay.headers["location"] == first.headers["location"]
        current = client.get(first.headers["location"], headers=headers)
        assert current.status_code == 200 and current.json()["status"] == "completed"
    with Session(work.engine) as session:
        assert session.scalar(select(func.count()).select_from(ExecutionJob)) == 1
        assert session.scalar(select(func.count()).select_from(RunIdempotency)) == 1
        assert session.get(Run, work.run).status == "queued"  # old run never enqueued


def test_real_two_processes_do_not_share_an_active_lease(work):
    schedule(work)
    workers = [worker_process(work), worker_process(work)]
    try:
        for worker in workers:
            assert worker.wait(timeout=10) == 0
        result = state(work)
        assert result.status == "completed" and result.attempts == 1
        assert [e[2] for e in result.events] == [
            "preparing",
            "starting",
            "running",
            "completed",
        ]
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=5)


def test_delayed_runner_result_cannot_overwrite_new_owner_completion(work):
    schedule(work)
    config = settings()
    old = claim(work.engine, config)
    assert not tick(work.engine, old, config, SimulatedRunner())
    assert not tick(work.engine, old, config, SimulatedRunner())
    entered, release = Event(), Event()

    class DelayedRunner(SimulatedRunner):
        def start(self, request, now):
            entered.set()
            assert release.wait(5)
            return "completed"

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(tick, work.engine, old, config, DelayedRunner())
        try:
            assert entered.wait(5)
            with Session(work.engine) as session, session.begin():
                session.get(ExecutionJob, old.job_id).lease_until = session.scalar(
                    select(func.clock_timestamp())
                ) - timedelta(seconds=1)
            new = claim(work.engine, config)
            assert new.fence > old.fence
            process(work.engine, new, config, Event())
            completed = state(work)
            assert completed.status == "completed"
            release.set()
            with pytest.raises(LostLease):
                future.result(timeout=5)
            assert state(work).events == completed.events
            assert state(work).status == "completed"
        finally:
            release.set()


@pytest.mark.parametrize("phase", ["queued", "preparing", "starting", "running"])
def test_failure_allowed_from_each_nonterminal_state(work, phase):
    schedule(work)
    lease = claim(work.engine, settings())
    for target in ["preparing", "starting", "running"]:
        if state(work).status == phase:
            break
        transition(work.engine, lease, target)
    transition(work.engine, lease, "failed", "retries_exhausted")
    result = state(work)
    assert result.status == result.job_state == "failed"
    with pytest.raises(LostLease):
        transition(work.engine, lease, "preparing")
    assert state(work).events == result.events


def test_transition_event_and_job_finish_rollback_together(work, monkeypatch):
    from sqlalchemy.exc import OperationalError

    import lavan.jobs as jobs

    schedule(work)
    lease = claim(work.engine, settings())
    transition(work.engine, lease, "preparing")
    transition(work.engine, lease, "starting")
    transition(work.engine, lease, "running")
    before = state(work)

    class FailingSession(Session):
        def flush(self, *args, **kwargs):
            failing = any(isinstance(obj, RunEvent) for obj in self.new)
            super().flush(*args, **kwargs)
            if failing:
                raise OperationalError("private", {}, Exception("private"))

    with monkeypatch.context() as patch:
        patch.setattr(jobs, "Session", FailingSession)
        with pytest.raises(OperationalError):
            transition(work.engine, lease, "completed")
    after = state(work)
    assert after.status == "running" and after.job_state == "leased"
    assert after.events == before.events
    transition(work.engine, lease, "completed")
    assert state(work).status == "completed"


def test_enqueue_management_command_is_explicit_and_idempotent(
    work, monkeypatch, capsys
):
    import json

    from lavan.manage import main

    monkeypatch.setenv("POSTGRES_DB", work.name)
    assert main(["enqueue-run", str(uuid4())]) == 1
    assert "not found" in capsys.readouterr().err
    assert main(["enqueue-run", str(work.run)]) == 0
    first = json.loads(capsys.readouterr().out)
    assert main(["enqueue-run", str(work.run)]) == 0
    assert json.loads(capsys.readouterr().out) == first
    assert first["scheduled"] == "simulation"
