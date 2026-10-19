"""Durable simulated worker. No source access, execution, or listener is created."""

import argparse
import logging
import signal
from threading import Event
from uuid import uuid4

from sqlalchemy.orm import Session

from lavan.config import Settings
from lavan.db import database_engine
from lavan.jobs import LostLease, claim, move, now, owned, renew, retry
from lavan.models import Artifact
from lavan.runner import RunnerRequest, SimulatedRunner

log = logging.getLogger(__name__)


def tick(engine, lease, settings, runner):
    # Snapshot the runner request and renew in a short transaction, then close it.
    with Session(engine) as session, session.begin():
        job, run = owned(session, lease)
        renew(session, job, lease, settings)
        cancelling = run.cancellation_requested_at is not None
        if not cancelling and run.status == "queued":
            move(session, job, run, lease, "preparing")
            return False
        if not cancelling and run.status == "preparing":
            move(session, job, run, lease, "starting")
            return False
        artifact = session.get(Artifact, run.artifact_id)
        request = RunnerRequest(
            run_id=run.id,
            artifact_id=artifact.id,
            artifact_sha256=artifact.sha256,
            runtime_template=run.runtime_template,
            operation_id=f"run:{run.id}:start:v1",
            fence=lease.fence,
            attempt=job.attempts,
            accepted_at=job.accepted_at,
            scenario=job.scenario,
            startup_seconds=job.startup_seconds,
            duration_seconds=job.duration_seconds,
            stop_requested_at=run.cancellation_requested_at,
        )
        status = run.status
        clock = now(session)
    # Both operations are bounded/nonblocking in the simulator. A future remote
    # adapter must provide deadlines and nonblocking reconciliation, not long RPCs.
    result = (
        runner.stop(request, clock)
        if cancelling
        else runner.start(request, clock)
        if status == "starting"
        else runner.inspect(request, clock)
    )
    if result not in {"starting", "running", "completed", "startup_failed", "stopped"}:
        raise ValueError("Unknown runner result")
    with Session(engine) as session, session.begin():
        job, run = owned(session, lease)
        if run.cancellation_requested_at is not None:
            if cancelling and result == "stopped":
                move(session, job, run, lease, "stopped", stop_acknowledged=True)
                return True
            return False  # intent won; discard a normal result and stop next tick
        if result == "stopped":
            raise ValueError("Unrequested runner stop")
        if result == "startup_failed":
            move(session, job, run, lease, "failed", "simulated_startup_failure")
            return True
        if run.status == "starting" and result in {"running", "completed"}:
            move(session, job, run, lease, "running")
        elif run.status == "running" and result == "completed":
            move(session, job, run, lease, "completed")
            return True
    return False


def process(engine, lease, settings, stop, runner=None):
    runner = runner or SimulatedRunner()
    while not stop.is_set():
        try:
            if tick(engine, lease, settings, runner):
                return
        except LostLease:
            return
        except Exception:
            # Do not log exception strings, SQL parameters, or runner payloads.
            log.warning(
                "worker_error job=%s run=%s fence=%s",
                lease.job_id,
                lease.run_id,
                lease.fence,
            )
            try:
                retry(engine, lease, settings)
            except Exception:
                log.warning("retry_deferred job=%s run=%s", lease.job_id, lease.run_id)
            return
        stop.wait(min(settings.worker_poll_seconds, settings.worker_lease_seconds / 3))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once", action="store_true", help="Claim at most one job, then exit"
    )
    args = parser.parse_args(argv)
    settings = Settings()
    stop = Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    engine = database_engine()
    owner = uuid4()
    logging.basicConfig(level=logging.INFO)
    try:
        while not stop.is_set():
            try:
                lease = claim(engine, settings, owner)
            except Exception:
                log.warning("claim_unavailable")
                if args.once:
                    return 1
                stop.wait(settings.worker_poll_seconds)
                continue
            if lease:
                log.info(
                    "claimed job=%s run=%s fence=%s",
                    lease.job_id,
                    lease.run_id,
                    lease.fence,
                )
                process(engine, lease, settings, stop)
            if args.once:
                return 0
            stop.wait(settings.worker_poll_seconds)
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
