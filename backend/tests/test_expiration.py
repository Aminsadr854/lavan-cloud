"""PostgreSQL-time expiration, shared stop arbitration, and process recovery."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from test_cancellation import api as api
from test_worker import (
    database_clock,
    schedule,
    settings,
    state,
    wait_for,
)
from test_worker import work as work

from lavan.config import Settings
from lavan.jobs import (
    CancellationPending,
    LostLease,
    claim,
    discover_due,
    enqueue,
    move,
    owned,
    retry,
    transition,
)
from lavan.models import ExecutionJob, Run, RunIdempotency
from lavan.runner import SimulatedRunner
from lavan.runs import RunResponse, request_fingerprint
from lavan.worker import process, tick


def due(work, run_id=None):
    with Session(work.engine) as session, session.begin():
        assert work.name.startswith("lavan_m2_test_")
        run = session.get(Run, run_id or work.run)
        run.expires_at = run.created_at + timedelta(microseconds=1)
        deadline = run.expires_at
    wait_for(lambda: database_clock(work) >= deadline)
    return deadline


def future(work, seconds=5):
    with Session(work.engine) as session, session.begin():
        run = session.get(Run, work.run)
        run.expires_at = database_clock(work) + timedelta(seconds=seconds)
        return run.expires_at


def details(work):
    with Session(work.engine) as session:
        run = session.get(Run, work.run)
        return (
            run.expires_at,
            run.expiration_requested_at,
            run.cancellation_requested_at,
        )


def labels(work):
    return [event[2] for event in state(work).events]


@pytest.fixture
def short_api(work, monkeypatch, request):
    monkeypatch.setenv("RUN_MAX_LIFETIME_SECONDS", "2")
    return request.getfixturevalue("api")


def test_new_deadline_uses_pg_time_and_replay_does_not_extend(work, short_api):
    client, headers = short_api
    auth = {**headers[0], "Idempotency-Key": uuid4().hex}
    payload = {
        "artifact_id": str(work.artifact),
        "runtime_template": "python-fastapi-v1",
    }
    path = f"/v1/projects/{work.project}/runs"
    first = client.post(path, headers=auth, json=payload)
    assert first.status_code == 201
    body = first.json()
    assert body["expires_at"] is not None and body["expiration_requested_at"] is None
    with Session(work.engine) as session:
        run = session.get(Run, UUID(body["id"]))
        assert run.expires_at - run.created_at == timedelta(seconds=2)
        saved = run.expires_at
    client.app.state.settings.run_max_lifetime_seconds = 900
    replay = client.post(path, headers=auth, json=payload)
    assert replay.status_code == 201 and replay.content == first.content
    assert replay.headers["location"] == first.headers["location"]
    current = client.get(first.headers["location"], headers=headers[0]).json()
    assert (
        current["expires_at"] == body["expires_at"]
        and current["expiration_requested_at"] is None
    )
    for field in [
        "expires_at",
        "expiration_requested_at",
        "ttl",
        "run_max_lifetime_seconds",
    ]:
        assert (
            client.post(path, headers=auth, json={**payload, field: 42}).status_code
            == 422
        )
    with Session(work.engine) as session:
        assert session.get(Run, UUID(body["id"])).expires_at == saved


@pytest.mark.parametrize("value", [0, -1, 86401, float("nan"), float("inf")])
def test_lifetime_settings_are_bounded_and_positive(value):
    with pytest.raises(ValueError):
        Settings(run_max_lifetime_seconds=value)


def test_legacy_snapshot_body_and_null_deadline_are_preserved(work, api):
    client, headers = api
    key = uuid4().hex
    with Session(work.engine) as session, session.begin():
        run = session.get(Run, work.run)
        snapshot = RunResponse.model_validate(run).model_dump(mode="json")
        session.add(
            RunIdempotency(
                user_id=work.user,
                operation="run.create",
                key=key,
                fingerprint=request_fingerprint(
                    work.project, work.artifact, "python-fastapi-v1"
                ),
                run_id=work.run,
                creation_response=snapshot,
            )
        )
    with Session(work.engine) as session:
        before = session.scalar(
            text(
                "SELECT creation_response::text FROM run_idempotency WHERE run_id=:id"
            ),
            {"id": work.run},
        )
    response = client.post(
        f"/v1/projects/{work.project}/runs",
        headers={**headers[0], "Idempotency-Key": key},
        json={
            "artifact_id": str(work.artifact),
            "runtime_template": "python-fastapi-v1",
        },
    )
    assert response.status_code == 201 and response.json() == snapshot
    assert "expires_at" not in response.json() and discover_due(work.engine) == 0
    with Session(work.engine) as session:
        assert session.get(Run, work.run).expires_at is None
        assert (
            session.scalar(
                text(
                    "SELECT creation_response::text FROM run_idempotency "
                    "WHERE run_id=:id"
                ),
                {"id": work.run},
            )
            == before
        )
        assert session.scalar(select(func.count()).select_from(ExecutionJob)) == 0


@pytest.mark.parametrize("scheduled", [False, True])
def test_never_attempted_due_run_expires_without_runner_or_enqueue(work, scheduled):
    if scheduled:
        schedule(work)
    due(work)
    with Session(work.engine) as session, session.begin():
        with pytest.raises(LookupError, match="deadline"):
            enqueue(session, work.run)
    assert discover_due(work.engine) == 1
    assert state(work).status == "expired"
    assert labels(work) == ["expire_requested", "expired"]
    assert state(work).attempts == (0 if scheduled else None)
    assert details(work)[1] is not None and details(work)[2] is None
    assert claim(work.engine, settings()) is None


@pytest.mark.parametrize("phase", ["preparing", "starting", "running"])
def test_due_active_phases_require_stop_acknowledgement(work, phase):
    schedule(work)
    config = settings(worker_lease_seconds=5)
    lease = claim(work.engine, config)
    for target in ["preparing", "starting", "running"]:
        transition(work.engine, lease, target)
        if target == phase:
            break
    deadline = due(work)
    discover_due(work.engine)
    assert state(work).status == phase

    class Ack(SimulatedRunner):
        def stop(self, request, clock):
            assert request.operation_id == f"run:{work.run}:start:v1"
            assert request.stop_requested_at is not None
            with Session(work.engine) as session, session.begin():
                session.scalar(
                    select(ExecutionJob)
                    .where(ExecutionJob.id == lease.job_id)
                    .with_for_update(nowait=True)
                )
            return super().stop(request, clock)

    assert tick(work.engine, lease, config, Ack())
    assert state(work).status == "expired" and state(work).job_state == "expired"
    assert details(work) == (deadline, details(work)[1], None)
    assert labels(work)[-2:] == ["expire_requested", "expired"]


@pytest.mark.parametrize("cancel_first", [True, False])
def test_stop_arbitration_and_late_cancel_get_fields(work, api, cancel_first):
    schedule(work)
    config = settings(worker_lease_seconds=5)
    lease = claim(work.engine, config)
    transition(work.engine, lease, "preparing")
    future(work)
    client, headers = api
    path = f"/v1/projects/{work.project}/runs/{work.run}"
    if cancel_first:
        assert client.post(path + "/cancel", headers=headers[0]).status_code == 202
    due(work)
    response = client.post(path + "/cancel", headers=headers[0])
    assert response.status_code == 202
    expected = "stopped" if cancel_first else "expired"
    assert (response.json()["cancellation_requested_at"] is not None) == cancel_first
    assert (response.json()["expiration_requested_at"] is not None) != cancel_first
    assert tick(work.engine, lease, config, SimulatedRunner())
    final = client.get(path, headers=headers[0]).json()
    assert final["status"] == expected
    assert labels(work)[-2:] == (
        ["cancel_requested", "stopped"]
        if cancel_first
        else ["expire_requested", "expired"]
    )
    assert client.post(path + "/cancel", headers=headers[0]).json() == final


@pytest.mark.parametrize("terminal", ["completed", "failed"])
def test_terminal_before_deadline_is_not_relabelled(work, terminal):
    schedule(work)
    config = settings(worker_lease_seconds=5)
    future(work, 0.5)
    lease = claim(work.engine, config)
    if terminal == "completed":
        for target in ["preparing", "starting", "running"]:
            transition(work.engine, lease, target)
    transition(
        work.engine,
        lease,
        terminal,
        "simulated_startup_failure" if terminal == "failed" else None,
    )
    before = state(work).events
    wait_for(lambda: database_clock(work) >= details(work)[0])
    assert discover_due(work.engine) == 0
    assert state(work).status == terminal and state(work).events == before
    assert details(work)[1:] == (None, None)


def test_competing_sweeps_do_not_duplicate_intent_or_terminal_events(work):
    schedule(work)
    due(work)
    barrier = Barrier(2, timeout=5)

    def sweep(_):
        barrier.wait()
        return discover_due(work.engine)

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(sweep, range(2)))
    assert labels(work) == ["expire_requested", "expired"]
    assert discover_due(work.engine) == 0


def test_due_retry_backoff_and_exhausted_budget_are_reconciled(work):
    schedule(work)
    future(work)
    with Session(work.engine) as session, session.begin():
        job = session.scalar(select(ExecutionJob))
        job.attempts = job.max_attempts
        job.fence = 3
        job.available_at = database_clock(work) + timedelta(hours=1)
    due(work)
    discover_due(work.engine)
    lease = claim(work.engine, settings(worker_lease_seconds=5))
    assert lease is not None
    process(work.engine, lease, settings(), Event())
    assert state(work).status == "expired" and state(work).attempts == 3


def test_deferred_deadline_guard_rejects_late_commit_then_recovers(work):
    schedule(work)
    config = settings(worker_lease_seconds=5)
    lease = claim(work.engine, config)
    for target in ["preparing", "starting", "running"]:
        transition(work.engine, lease, target)
    deadline = future(work, 0.2)
    with pytest.raises(IntegrityError) as error:
        with Session(work.engine) as session, session.begin():
            job, run = owned(session, lease)
            assert move(session, job, run, lease, "completed")
            session.flush()
            # Deliberately delayed commit, not a production runner wait.
            wait_for(lambda: database_clock(work) >= deadline)
    assert error.value.orig.diag.constraint_name == "ck_runs_deadline_commit"
    assert state(work).status == "running" and "completed" not in labels(work)
    retry(work.engine, lease, config)
    recovered = wait_for(lambda: claim(work.engine, config))
    process(work.engine, recovered, config, Event())
    assert state(work).status == "expired"


def test_atomic_update_rechecks_deadline_after_prior_inspection(work, monkeypatch):
    import lavan.jobs as jobs

    schedule(work)
    config = settings(worker_lease_seconds=5)
    lease = claim(work.engine, config)
    for target in ["preparing", "starting", "running"]:
        transition(work.engine, lease, target)
    deadline = future(work, 0.2)

    class PausedWrite(Session):
        def scalar(self, statement, *args, **kwargs):
            if (
                getattr(statement, "is_update", False)
                and statement.table.name == "runs"
            ):
                wait_for(lambda: database_clock(work) >= deadline)
            return super().scalar(statement, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(jobs, "Session", PausedWrite)
        with pytest.raises(CancellationPending):
            transition(work.engine, lease, "completed")
    assert state(work).status == "running" and labels(work)[-1] == "expire_requested"
    assert tick(work.engine, lease, config, SimulatedRunner())
    assert state(work).status == "expired"


def test_slow_stop_does_not_block_discovery_of_other_due_runs(work):
    schedule(work)
    config = settings(
        worker_lease_seconds=5,
        worker_expiration_poll_seconds=0.02,
        worker_expiration_batch_size=1,
    )
    lease = claim(work.engine, config)
    for target in ["preparing", "starting"]:
        transition(work.engine, lease, target)
    due(work)
    with Session(work.engine) as session, session.begin():
        created = database_clock(work)
        other = Run(
            project_id=work.project,
            artifact_id=work.artifact,
            runtime_template="python-fastapi-v1",
            created_at=created,
            expires_at=created + timedelta(seconds=0.2),
        )
        session.add(other)
        session.flush()
        other_id = other.id
        session.add(ExecutionJob(run_id=other_id))
    calls = []

    class SlowAck(SimulatedRunner):
        def stop(self, request, clock):
            calls.append(True)
            with Session(work.engine) as session:
                if session.get(Run, other_id).status != "expired":
                    return "starting"
            return super().stop(request, clock)

    stop = Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future_result = executor.submit(
            process, work.engine, lease, config, stop, SlowAck()
        )
        try:
            future_result.result(timeout=5)
        finally:
            stop.set()
    assert state(work).status == "expired" and len(calls) > 1
    with Session(work.engine) as session:
        assert session.get(Run, other_id).status == "expired"
        assert (
            session.scalar(
                select(ExecutionJob).where(ExecutionJob.run_id == other_id)
            ).attempts
            == 0
        )


def test_stale_stop_ack_cannot_overwrite_expired(work):
    schedule(work)
    config = settings(worker_lease_seconds=5)
    old = claim(work.engine, config)
    transition(work.engine, old, "preparing")
    due(work)
    ready, release = Event(), Event()

    class LateStop(SimulatedRunner):
        def stop(self, request, clock):
            ready.set()
            assert release.wait(5)
            return super().stop(request, clock)

    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(tick, work.engine, old, config, LateStop())
        try:
            assert ready.wait(5)
            with Session(work.engine) as session, session.begin():
                session.get(ExecutionJob, old.job_id).lease_until = database_clock(
                    work
                ) - timedelta(seconds=1)
            new = claim(work.engine, config)
            assert tick(work.engine, new, config, SimulatedRunner())
            before = state(work).events
            release.set()
            with pytest.raises(LostLease):
                result.result(timeout=5)
            assert state(work).status == "expired" and state(work).events == before
        finally:
            release.set()


@pytest.mark.parametrize("local_clock", ["-1000000000", "9000000000"])
@pytest.mark.parametrize("overdue", [False, True])
def test_real_worker_restart_uses_postgres_not_local_wall_clock(
    work, local_clock, overdue
):
    import os
    import subprocess
    import sys

    schedule(work)
    due(work) if overdue else future(work, seconds=10)
    env = {
        **os.environ,
        "POSTGRES_DB": work.name,
        "WORKER_POLL_SECONDS": "0.02",
        "SIMULATOR_SCENARIO": "success",
        "SIMULATOR_STARTUP_SECONDS": "0",
        "SIMULATOR_DURATION_SECONDS": "0",
    }
    assert work.name.startswith("lavan_m2_test_")
    source = (
        "import time; time.time=lambda: " + local_clock + "; "
        "from lavan.worker import main; raise SystemExit(main(['--once']))"
    )
    result = subprocess.run(
        [sys.executable, "-c", source],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
    )
    assert result.returncode == 0
    assert state(work).status == ("expired" if overdue else "completed")
    if overdue:
        assert state(work).attempts == 0
        assert labels(work) == ["expire_requested", "expired"]


def test_sigkill_during_expiration_ack_is_recoverable(work):
    import os
    import select as io_select
    import subprocess
    import sys

    from test_worker import worker_process

    schedule(work)
    source = """
from lavan import worker
from lavan.runner import SimulatedRunner
class Hook(SimulatedRunner):
    def stop(self, request, clock):
        result = super().stop(request, clock)
        print('STOP_ACK', flush=True)
        input()
        return result
worker.SimulatedRunner = Hook
raise SystemExit(worker.main(['--once']))
"""
    env = {
        **os.environ,
        "POSTGRES_DB": work.name,
        "WORKER_LEASE_SECONDS": "0.4",
        "WORKER_POLL_SECONDS": "0.02",
        "SIMULATOR_SCENARIO": "slow_startup",
        "SIMULATOR_SLOW_STARTUP_SECONDS": "10",
    }
    assert work.name.startswith("lavan_m2_test_")
    worker = subprocess.Popen(
        [sys.executable, "-c", source],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    recovery = None
    try:
        wait_for(lambda: state(work).status == "starting")
        due(work)
        assert io_select.select([worker.stdout], [], [], 5)[0]
        assert worker.stdout.readline().strip() == "STOP_ACK"
        assert details(work)[1] is not None
        worker.kill()
        worker.wait(timeout=5)
        wait_for(lambda: state(work).lease_until <= database_clock(work))
        recovery = worker_process(work)
        assert recovery.wait(timeout=5) == 0
        assert state(work).status == "expired" and state(work).attempts == 2
        assert labels(work) == ["preparing", "starting", "expire_requested", "expired"]
        assert details(work)[2] is None
    finally:
        for proc in (worker, recovery):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        worker.stdin.close()
        worker.stdout.close()


def test_discovery_is_batched_and_uses_partial_due_index(work):
    with Session(work.engine) as session, session.begin():
        for _ in range(4):
            session.add(
                Run(
                    project_id=work.project,
                    artifact_id=work.artifact,
                    runtime_template="python-fastapi-v1",
                )
            )
    with work.engine.begin() as connection:
        assert work.name.startswith("lavan_m2_test_")
        connection.execute(
            text("UPDATE runs SET expires_at=created_at+interval '1 microsecond'")
        )
        connection.execute(text("SET LOCAL enable_seqscan=off"))
        plan = (
            connection.execute(
                text(
                    "EXPLAIN SELECT id FROM runs WHERE expires_at <= clock_timestamp() "
                    "AND expires_at IS NOT NULL AND cancellation_requested_at IS NULL "
                    "AND expiration_requested_at IS NULL "
                    "AND status IN ('queued','preparing','starting','running') "
                    "ORDER BY expires_at,id LIMIT 2"
                )
            )
            .scalars()
            .all()
        )
        assert any("ix_runs_expiration_due" in line for line in plan)
    assert discover_due(work.engine, 2) == 2
    assert discover_due(work.engine, 2) == 2
    assert discover_due(work.engine, 2) == 1
    assert discover_due(work.engine, 2) == 0


def test_retry_keeps_original_deadline(work):
    schedule(work)
    deadline = future(work, 10)
    config = settings(worker_retry_seconds=0.05)
    first = claim(work.engine, config)
    retry(work.engine, first, config)
    second = wait_for(lambda: claim(work.engine, config))
    assert second.fence > first.fence
    assert details(work)[0] == deadline
