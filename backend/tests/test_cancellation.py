"""Cancellation races, PostgreSQL persistence, and worker process recovery."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from time import monotonic
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_worker import (  # reuse only disposable-database helpers
    database_clock,
    schedule,
    settings,
    state,
    wait_for,
    worker_process,
)
from test_worker import work as work

from lavan.api import create_app
from lavan.jobs import (
    LostLease,
    claim,
    request_cancellation,
    retry,
    transition,
)
from lavan.models import (
    ApiToken,
    ExecutionJob,
    Project,
    Run,
    RunIdempotency,
    User,
)
from lavan.runner import SimulatedRunner
from lavan.tokens import new_token, token_digest
from lavan.worker import process, tick


class PrivateHeaders(dict):
    def __repr__(self):
        return "<private authentication headers>"


@pytest.fixture
def api(work, monkeypatch):
    monkeypatch.setenv("POSTGRES_DB", work.name)
    raw = [new_token() for _ in range(3)]
    with Session(work.engine) as session, session.begin():
        other = User()
        session.add(other)
        session.flush()
        session.add_all(
            [
                ApiToken(user_id=work.user, digest=token_digest(raw[0])),
                ApiToken(user_id=other.id, digest=token_digest(raw[1])),
                ApiToken(
                    user_id=work.user,
                    digest=token_digest(raw[2]),
                    revoked_at=database_clock(work),
                ),
            ]
        )
    with TestClient(create_app()) as client:
        # Raw credentials stay private; never include the harness in assertion output.
        yield client, [PrivateHeaders(Authorization="Bearer " + value) for value in raw]


def intent(work):
    with Session(work.engine) as session, session.begin():
        run, pending = request_cancellation(session, work.user, work.project, work.run)
        return run.status, pending, run.cancellation_requested_at


def events(work):
    return [value[2] for value in state(work).events]


def test_unscheduled_and_never_claimed_jobs_stop_in_request(work, api):
    client, headers = api
    path = f"/v1/projects/{work.project}/runs/{work.run}/cancel"
    first = client.post(path, headers=headers[0])
    assert first.status_code == 200 and first.json()["status"] == "stopped"
    assert first.json()["cancellation_requested_at"] is not None
    assert state(work).job_state is None
    assert events(work) == ["cancel_requested", "stopped"]
    assert client.post(path, headers=headers[0]).json() == first.json()
    assert events(work) == ["cancel_requested", "stopped"]
    assert claim(work.engine, settings()) is None


def test_scheduled_never_claimed_run_is_not_executed(work, api):
    schedule(work)
    client, headers = api
    response = client.post(
        f"/v1/projects/{work.project}/runs/{work.run}/cancel", headers=headers[0]
    )
    assert response.status_code == 200 and response.json()["status"] == "stopped"
    assert state(work).job_state == "stopped" and state(work).attempts == 0
    assert claim(work.engine, settings()) is None
    assert events(work) == ["cancel_requested", "stopped"]


@pytest.mark.parametrize("phase", ["queued", "preparing", "starting", "running"])
def test_previously_claimed_phases_require_ack_and_repeated_requests_are_safe(
    work, api, phase
):
    schedule(work)
    config = settings(worker_lease_seconds=5)
    lease = claim(work.engine, config)
    for target in ["preparing", "starting", "running"]:
        if state(work).status == phase:
            break
        transition(work.engine, lease, target)
    client, headers = api
    path = f"/v1/projects/{work.project}/runs/{work.run}"
    response = client.post(path + "/cancel", headers=headers[0])
    assert response.status_code == 202 and response.headers["location"] == path
    assert response.json()["status"] == phase
    timestamp = response.json()["cancellation_requested_at"]
    repeated = client.post(path + "/cancel", headers=headers[0])
    assert (
        repeated.status_code == 202
        and repeated.json()["cancellation_requested_at"] == timestamp
    )
    assert events(work).count("cancel_requested") == 1

    class AcknowledgingRunner(SimulatedRunner):
        def stop(self, request, clock):
            assert request.operation_id == f"run:{work.run}:start:v1"
            assert request.stop_requested_at is not None
            # No row locks may be held across stop acknowledgement.
            with Session(work.engine) as session, session.begin():
                session.scalar(
                    select(ExecutionJob)
                    .where(ExecutionJob.id == lease.job_id)
                    .with_for_update(nowait=True)
                )
                session.scalar(
                    select(Run).where(Run.id == work.run).with_for_update(nowait=True)
                )
            assert super().stop(request, clock) == "stopped"
            assert self.inspect(request, clock) == "stopped"
            return "stopped"

    assert tick(work.engine, lease, config, AcknowledgingRunner())
    current = client.get(path, headers=headers[0])
    assert current.status_code == 200 and current.json()["status"] == "stopped"
    assert current.json()["cancellation_requested_at"] == timestamp
    assert client.post(path + "/cancel", headers=headers[0]).status_code == 200
    assert events(work).count("stopped") == 1 and state(work).job_state == "stopped"
    with pytest.raises(LostLease):
        transition(work.engine, lease, "completed")


@pytest.mark.parametrize("outcome", ["completed", "failed"])
def test_already_terminal_is_unchanged(work, api, outcome):
    schedule(work)
    config = settings(
        simulator_scenario="startup_failure" if outcome == "failed" else "success"
    )
    lease = claim(work.engine, config)
    process(work.engine, lease, config, Event())
    before = state(work)
    client, headers = api
    response = client.post(
        f"/v1/projects/{work.project}/runs/{work.run}/cancel", headers=headers[0]
    )
    assert response.status_code == 200 and response.json()["status"] == outcome
    assert response.json()["cancellation_requested_at"] is None
    assert state(work).events == before.events and state(work).error == before.error


@pytest.mark.parametrize("credential", ["missing", "unknown", "revoked", "other"])
def test_cancel_authentication_and_ownership(work, api, credential):
    client, headers = api
    selected = (
        {}
        if credential == "missing"
        else (
            {"Authorization": "Bearer " + new_token()}
            if credential == "unknown"
            else headers[2 if credential == "revoked" else 1]
        )
    )
    response = client.post(
        f"/v1/projects/{work.project}/runs/{work.run}/cancel", headers=selected
    )
    assert response.status_code == (404 if credential == "other" else 401)
    if response.status_code == 401:
        assert response.headers["www-authenticate"] == "Bearer"
    else:
        assert response.json() == {"detail": "Not found"}
    assert events(work) == []


def test_cancel_resource_validation_and_no_client_fields(work, api):
    client, headers = api
    with Session(work.engine) as session, session.begin():
        project = Project(owner_id=work.user, name="Other project")
        session.add(project)
        session.flush()
        other = project.id
    for path in [
        f"/v1/projects/{work.project}/runs/{uuid4()}/cancel",
        f"/v1/projects/{other}/runs/{work.run}/cancel",
        f"/v1/projects/{uuid4()}/runs/{work.run}/cancel",
    ]:
        response = client.post(path, headers=headers[0])
        assert response.status_code == 404 and response.json() == {
            "detail": "Not found"
        }
    for project, run in [("bad", str(work.run)), (str(work.project), "bad")]:
        assert (
            client.post(
                f"/v1/projects/{project}/runs/{run}/cancel", headers=headers[0]
            ).status_code
            == 422
        )
    for body in [
        {"status": "stopped"},
        {"owner_id": str(work.user)},
        {"reason": "command"},
        {"run_id": str(work.run)},
        [],
    ]:
        assert (
            client.post(
                f"/v1/projects/{work.project}/runs/{work.run}/cancel",
                headers=headers[0],
                json=body,
            ).status_code
            == 422
        )
    assert events(work) == []


@pytest.mark.parametrize("result", ["completed", "startup_failed", "exception"])
def test_intent_commits_before_late_runner_result_and_blocks_normal_terminal(
    work, result
):
    schedule(work)
    config = settings(worker_lease_seconds=5)
    lease = claim(work.engine, config)
    for target in ["preparing", "starting", "running"]:
        transition(work.engine, lease, target)
    entered, release = Event(), Event()

    class DelayedRunner(SimulatedRunner):
        def inspect(self, request, clock):
            entered.set()
            assert release.wait(5)
            if result == "exception":
                raise RuntimeError("private fixture failure")
            return result

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(tick, work.engine, lease, config, DelayedRunner())
        try:
            assert entered.wait(5)
            assert intent(work)[1]
            release.set()
            if result == "exception":
                with pytest.raises(RuntimeError):
                    future.result(timeout=5)
                retry(work.engine, lease, config)
                lease = wait_for(lambda: claim(work.engine, config))
            else:
                assert future.result(timeout=5) is False
            assert state(work).status == "running"
            assert "completed" not in events(work) and "failed" not in events(work)
            process(work.engine, lease, config, Event())
            assert state(work).status == "stopped"
        finally:
            release.set()


def test_normal_terminal_commit_wins_before_blocked_cancel(work):
    schedule(work)
    lease = claim(work.engine, settings())
    for target in ["preparing", "starting", "running"]:
        transition(work.engine, lease, target)
    from lavan.jobs import move, owned

    # Hold the exact worker locks so cancellation is forced to linearize later.
    with ThreadPoolExecutor(max_workers=1) as executor:
        with Session(work.engine) as session, session.begin():
            job, run = owned(session, lease)
            entered = Event()

            def cancel():
                entered.set()
                return intent(work)

            future = executor.submit(cancel)
            assert entered.wait(5)
            move(session, job, run, lease, "completed")
        assert future.result(timeout=5) == ("completed", False, None)
    assert events(work) == ["preparing", "starting", "running", "completed"]


def test_concurrent_repeated_requests_record_one_intent_and_stop(work):
    schedule(work)
    barrier = Barrier(2, timeout=5)

    def cancel(_):
        barrier.wait()
        return intent(work)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(cancel, range(2)))
    assert all(result[0] == "stopped" and not result[1] for result in results)
    assert results[0][2] == results[1][2]
    assert events(work) == ["cancel_requested", "stopped"]


def test_attempted_queued_backoff_cancellation_bypasses_retry_budget(work):
    schedule(work)
    with Session(work.engine) as session, session.begin():
        job = session.scalar(select(ExecutionJob))
        job.attempts = job.max_attempts
        job.fence = 3
        job.available_at = database_clock(work) + timedelta(hours=1)
    assert claim(work.engine, settings()) is None
    assert intent(work)[1]  # queued is not proof that execution never started
    lease = claim(work.engine, settings())
    assert lease is not None

    class FailingStop(SimulatedRunner):
        def stop(self, *args):
            raise RuntimeError("private stop transport failure")

    process(work.engine, lease, settings(), Event(), FailingStop())
    assert state(work).status == "queued" and state(work).job_state == "pending"
    assert "failed" not in events(work)
    recovered = wait_for(lambda: claim(work.engine, settings()))
    process(work.engine, recovered, settings(), Event())
    assert state(work).status == "stopped" and state(work).attempts == 3


def test_real_worker_slow_start_cancellation_is_prompt(work):
    schedule(work)
    worker = worker_process(work, SIMULATOR_SLOW_STARTUP_SECONDS="10")
    try:
        wait_for(lambda: state(work).status == "starting")
        started = monotonic()
        assert intent(work)[1]
        wait_for(lambda: state(work).status == "stopped", timeout=2)
        assert monotonic() - started < 2
        assert worker.wait(timeout=5) == 0
        assert events(work) == ["preparing", "starting", "cancel_requested", "stopped"]
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)


def test_sigkill_after_intent_reconciles_on_recovery(work):
    import os
    import select as io_select
    import subprocess
    import sys

    schedule(work)
    # A real worker main, with a test-only stop hook that pauses outside SQL after
    # the simulator acknowledgement but before its result can be persisted.
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
        assert intent(work)[1]
        assert io_select.select([worker.stdout], [], [], 5)[0]
        assert worker.stdout.readline().strip() == "STOP_ACK"
        worker.kill()
        worker.wait(timeout=5)
        wait_for(lambda: state(work).lease_until <= database_clock(work))
        recovery = worker_process(work)
        assert recovery.wait(timeout=5) == 0
        result = state(work)
        assert result.status == "stopped" and result.attempts == 2
        assert events(work) == ["preparing", "starting", "cancel_requested", "stopped"]
    finally:
        for proc in (worker, recovery):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        worker.stdin.close()
        worker.stdout.close()


def test_creation_snapshot_replay_after_cancellation_stays_original(work, api):
    client, headers = api
    auth = {**headers[0], "Idempotency-Key": uuid4().hex}
    path = f"/v1/projects/{work.project}/runs"
    payload = {
        "artifact_id": str(work.artifact),
        "runtime_template": "python-fastapi-v1",
    }
    first = client.post(path, headers=auth, json=payload)
    assert first.status_code == 201 and "cancellation_requested_at" not in first.json()
    cancel = client.post(first.headers["location"] + "/cancel", headers=headers[0])
    assert cancel.status_code == 200 and cancel.json()["status"] == "stopped"
    replay = client.post(path, headers=auth, json=payload)
    assert replay.status_code == 201 and replay.json() == first.json()
    assert replay.headers["location"] == first.headers["location"]
    current = client.get(first.headers["location"], headers=headers[0])
    assert current.json() == cancel.json()
    with Session(work.engine) as session:
        record = session.scalar(
            select(RunIdempotency).where(
                RunIdempotency.run_id == UUID(first.json()["id"])
            )
        )
        assert record.creation_response == first.json()
        assert session.scalar(select(func.count()).select_from(ExecutionJob)) == 1


def test_intent_and_immediate_stop_rollback_as_one_transaction(work, api, monkeypatch):
    from sqlalchemy.exc import OperationalError

    import lavan.auth as auth

    schedule(work)

    class FailingSession(Session):
        def commit(self):
            self.flush()
            raise OperationalError("private", {}, Exception("private"))

    client, headers = api
    monkeypatch.setattr(auth, "Session", FailingSession)
    response = client.post(
        f"/v1/projects/{work.project}/runs/{work.run}/cancel", headers=headers[0]
    )
    assert response.status_code == 503 and response.json() == {
        "detail": "Service unavailable"
    }
    assert state(work).status == "queued" and state(work).job_state == "pending"
    assert events(work) == []
    with Session(work.engine) as session:
        assert session.get(Run, work.run).cancellation_requested_at is None


def test_shared_transition_refuses_unacknowledged_stop_or_late_failure(work):
    from lavan.jobs import CancellationPending, InvalidTransition

    schedule(work)
    config = settings(worker_lease_seconds=5)
    lease = claim(work.engine, config)
    with pytest.raises(InvalidTransition):
        transition(work.engine, lease, "stopped")
    assert intent(work)[1]
    for target in ["preparing", "failed"]:
        with pytest.raises(CancellationPending):
            transition(work.engine, lease, target)
    assert events(work) == ["cancel_requested"]
    assert tick(work.engine, lease, config, SimulatedRunner())
    with pytest.raises(LostLease):
        retry(work.engine, lease, config)
    assert events(work) == ["cancel_requested", "stopped"]


def test_explicit_enqueue_race_retries_without_reversing_lock_order(work):
    ready, release = Event(), Event()
    probes = []

    class GapSession(Session):
        def scalar(self, statement, *args, **kwargs):
            result = super().scalar(statement, *args, **kwargs)
            if getattr(statement, "_for_update_arg", None) is not None and any(
                item.get("entity") is ExecutionJob
                for item in getattr(statement, "column_descriptions", [])
            ):
                probes.append(True)
                if result is None and len(probes) == 1:
                    ready.set()
                    assert release.wait(5)
            return result

    def cancel():
        with GapSession(work.engine) as session:
            run, pending = request_cancellation(
                session, work.user, work.project, work.run
            )
            result = run.status, pending
            session.commit()
            return result

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(cancel)
        try:
            assert ready.wait(5)
            schedule(work)
            release.set()
            assert future.result(timeout=5) == ("stopped", False)
        finally:
            release.set()
    assert len(probes) == 2
    assert state(work).job_state == "stopped" and state(work).attempts == 0
    assert events(work) == ["cancel_requested", "stopped"]


def test_late_stop_ack_from_stale_worker_cannot_append_or_revive(work):
    schedule(work)
    config = settings(worker_lease_seconds=5)
    old = claim(work.engine, config)
    for target in ["preparing", "starting"]:
        transition(work.engine, old, target)
    assert intent(work)[1]
    ready, release = Event(), Event()

    class LateStop(SimulatedRunner):
        def stop(self, request, clock):
            ready.set()
            assert release.wait(5)
            return super().stop(request, clock)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(tick, work.engine, old, config, LateStop())
        try:
            assert ready.wait(5)
            with Session(work.engine) as session, session.begin():
                session.get(ExecutionJob, old.job_id).lease_until = database_clock(
                    work
                ) - timedelta(seconds=1)
            new = claim(work.engine, config)
            assert new.fence > old.fence
            assert tick(work.engine, new, config, SimulatedRunner())
            stopped = state(work)
            release.set()
            with pytest.raises(LostLease):
                future.result(timeout=5)
            assert (
                state(work).status == "stopped" and state(work).events == stopped.events
            )
        finally:
            release.set()
