"""Short, fenced PostgreSQL transactions. Runner calls belong outside this module."""

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from lavan.models import ExecutionJob, Project, Run, RunEvent

TERMINAL = {"completed", "failed", "stopped", "expired"}
TRANSITIONS = {
    "queued": {"preparing", "failed", "stopped", "expired"},
    "preparing": {"starting", "failed", "stopped", "expired"},
    "starting": {"running", "failed", "stopped", "expired"},
    "running": {"completed", "failed", "stopped", "expired"},
    "completed": set(),
    "failed": set(),
    "stopped": set(),
    "expired": set(),
}


class LostLease(Exception):
    """Ownership expired or was replaced; no mutation is permitted."""


class CancellationPending(Exception):
    """A normal result cannot supersede a selected durable stop intent."""


class CancellationBusy(Exception):
    pass


class InvalidTransition(Exception):
    pass


@dataclass(frozen=True)
class Lease:
    job_id: UUID
    run_id: UUID
    owner: UUID
    fence: int


def now(session):
    return session.scalar(select(func.clock_timestamp()))


def owned(session, lease):
    session.execute(text("SET LOCAL lock_timeout = '1000ms'"))
    job = session.scalar(
        select(ExecutionJob).where(ExecutionJob.id == lease.job_id).with_for_update()
    )
    if job is None:
        raise LostLease()
    run = session.scalar(select(Run).where(Run.id == job.run_id).with_for_update())
    verify(session, job, lease)
    return job, run


def verify(session, job, lease):
    clock = now(session)
    if (
        job.state != "leased"
        or job.lease_owner != lease.owner
        or job.fence != lease.fence
        or job.lease_until <= clock
    ):
        raise LostLease()
    return clock


def renew(session, job, lease, settings):
    # Check expiry in the UPDATE itself: a paused owner must never extend an
    # already expired lease just because it loaded the row while still valid.
    expiry = session.scalar(
        update(ExecutionJob)
        .where(
            ExecutionJob.id == lease.job_id,
            ExecutionJob.state == "leased",
            ExecutionJob.lease_owner == lease.owner,
            ExecutionJob.fence == lease.fence,
            ExecutionJob.lease_until > func.clock_timestamp(),
        )
        .values(
            lease_until=func.clock_timestamp()
            + timedelta(seconds=settings.worker_lease_seconds)
        )
        .returning(ExecutionJob.lease_until)
        .execution_options(synchronize_session=False)
    )
    if expiry is None:
        raise LostLease()
    set_committed_value(job, "lease_until", expiry)


def append_event(session, run, status, clock, error_code=None):
    # The caller holds the run row lock, including for non-transition intent events.
    sequence = (
        session.scalar(
            select(func.coalesce(func.max(RunEvent.sequence), 0)).where(
                RunEvent.run_id == run.id
            )
        )
        + 1
    )
    session.add(
        RunEvent(
            run_id=run.id,
            sequence=sequence,
            status=status,
            error_code=error_code,
            created_at=clock,
        )
    )


def _record_transition(session, run, target, clock, error_code=None):
    if target not in TRANSITIONS[run.status]:
        raise InvalidTransition()
    append_event(session, run, target, clock, error_code)
    run.status, run.updated_at, run.error_code = target, clock, error_code


def deadline_due(run, clock):
    return run.expires_at is not None and run.expires_at <= clock


def stop_target(run):
    if run.cancellation_requested_at is not None:
        return "stopped"
    if run.expiration_requested_at is not None:
        return "expired"
    return None


def stop_stamp(run):
    return run.cancellation_requested_at or run.expiration_requested_at


def select_stop_intent(session, run, *, cancel=False):
    """Single arbitration point, called only with the run row locked."""
    if run.status in TERMINAL:
        return stop_target(run)
    selected = stop_target(run)
    if selected:
        return selected
    clock = now(session)
    if deadline_due(run, clock):
        run.expiration_requested_at = clock
        marker, selected = "expire_requested", "expired"
    elif cancel:
        run.cancellation_requested_at = clock
        marker, selected = "cancel_requested", "stopped"
    else:
        return None
    run.updated_at = clock
    append_event(session, run, marker, clock)
    return selected


def reconcile_stop(session, job, run, *, cancel=False):
    """Persist intent, finalize provably unstarted runs, or wake reconciliation."""
    if run.status in TERMINAL:
        return False
    target = select_stop_intent(session, run, cancel=cancel)
    if target is None:
        return False
    clock = now(session)
    never_started = run.status == "queued" and (
        job is None
        or (
            job.state == "pending"
            and job.attempts == 0
            and job.fence == 0
            and job.accepted_at is None
        )
    )
    if never_started:
        _record_transition(session, run, target, clock)
        if job is not None:
            job.state = target
        return False
    if job is None:
        job = ExecutionJob(run_id=run.id)
        session.add(job)
    if job.state == "pending":
        job.available_at = clock
    return True


def move(session, job, run, lease, target, error_code=None, *, stop_acknowledged=False):
    """Shared worker transition path; false commits a newly selected stop intent."""
    clock = verify(session, job, lease)
    selected = select_stop_intent(session, run)
    if selected and target != selected:
        return False
    if target in {"stopped", "expired"} and (
        not stop_acknowledged or target != selected
    ):
        raise InvalidTransition()
    if target not in TRANSITIONS[run.status]:
        raise InvalidTransition()
    if error_code not in {None, "simulated_startup_failure", "retries_exhausted"}:
        raise ValueError("Unsupported sanitized error code")
    if selected is None:
        # Atomic PostgreSQL-time guard at the write, not just a prior inspection.
        changed = session.scalar(
            update(Run)
            .where(
                Run.id == run.id,
                Run.cancellation_requested_at.is_(None),
                Run.expiration_requested_at.is_(None),
                or_(Run.expires_at.is_(None), Run.expires_at > func.clock_timestamp()),
            )
            .values(
                status=target, updated_at=func.clock_timestamp(), error_code=error_code
            )
            .returning(Run.updated_at)
            .execution_options(synchronize_session=False)
        )
        if changed is None:
            select_stop_intent(session, run)
            return False
        append_event(session, run, target, changed, error_code)
        set_committed_value(run, "status", target)
        set_committed_value(run, "updated_at", changed)
        set_committed_value(run, "error_code", error_code)
    else:
        _record_transition(session, run, target, clock, error_code)
    if target == "starting" and job.accepted_at is None:
        job.accepted_at = clock
    verify(session, job, lease)
    if target in TERMINAL:
        job.state = target
        job.lease_owner = job.lease_until = None
    return True


def request_cancellation(session, user_id, project_id, run_id):
    """Owner operation. Lock job then run, matching worker order."""
    query = (
        select(Run)
        .join(Project, Run.project_id == Project.id)
        .where(
            Run.id == run_id, Run.project_id == project_id, Project.owner_id == user_id
        )
    )
    for _ in range(2):
        session.execute(text("SET LOCAL lock_timeout = '1000ms'"))
        if session.scalar(query) is None:
            raise LookupError()
        job = session.scalar(
            select(ExecutionJob).where(ExecutionJob.run_id == run_id).with_for_update()
        )
        run = session.scalar(
            query.with_for_update(of=Run).execution_options(populate_existing=True)
        )
        if run is None:
            raise LookupError()
        if (
            job is None
            and session.scalar(
                select(ExecutionJob.id).where(ExecutionJob.run_id == run_id)
            )
            is not None
        ):
            # Explicit enqueue won the formerly unscheduled run lock. Retry in
            # job/run order, never wait for its job while holding the run lock.
            session.rollback()
            continue
        return run, reconcile_stop(session, job, run, cancel=True)
    raise CancellationBusy()


def transition(engine, lease, target, error_code=None):
    with Session(engine) as session, session.begin():
        job, run = owned(session, lease)
        applied = move(session, job, run, lease, target, error_code)
    if not applied:
        raise CancellationPending()


def claim(engine, settings, owner=None):
    owner = owner or uuid4()
    with Session(engine) as session, session.begin():
        session.execute(text("SET LOCAL lock_timeout = '1000ms'"))
        clock = now(session)
        job = session.scalar(
            select(ExecutionJob)
            .where(
                or_(
                    and_(
                        ExecutionJob.state == "pending",
                        ExecutionJob.available_at <= clock,
                    ),
                    and_(
                        ExecutionJob.state == "leased",
                        ExecutionJob.lease_until <= clock,
                    ),
                )
            )
            .order_by(ExecutionJob.available_at, ExecutionJob.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            return None
        run = session.scalar(select(Run).where(Run.id == job.run_id).with_for_update())
        if run.status not in TERMINAL:
            reconcile_stop(session, job, run)
        if job.state in TERMINAL:
            return None
        # Set a new fencing identity even when finalizing an exhausted crash lease.
        job.fence += 1
        job.state = "leased"
        job.lease_owner = owner
        job.lease_until = now(session) + timedelta(
            seconds=settings.worker_lease_seconds
        )
        lease = Lease(job.id, run.id, owner, job.fence)
        if run.status in TERMINAL:
            verify(session, job, lease)
            job.state = run.status
            job.lease_owner = job.lease_until = None
            return None
        if job.attempts >= job.max_attempts and stop_target(run) is None:
            if not move(session, job, run, lease, "failed", "retries_exhausted"):
                job.state = "pending"
                job.lease_owner = job.lease_until = None
                job.available_at = now(session)
            return None
        job.attempts = min(job.max_attempts, job.attempts + 1)
        if job.scenario is None:
            job.scenario = settings.simulator_scenario
            job.startup_seconds = (
                settings.simulator_slow_startup_seconds
                if job.scenario == "slow_startup"
                else settings.simulator_startup_seconds
            )
            job.duration_seconds = settings.simulator_duration_seconds
        return lease


def retry(engine, lease, settings):
    with Session(engine) as session, session.begin():
        job, run = owned(session, lease)
        job.last_error_code = "worker_error"
        select_stop_intent(session, run)
        if stop_target(run) is not None:
            verify(session, job, lease)
            job.available_at = now(session) + timedelta(
                seconds=min(0.25, settings.worker_retry_seconds)
            )
            job.state = "pending"
            job.lease_owner = job.lease_until = None
        elif job.attempts >= job.max_attempts:
            if not move(session, job, run, lease, "failed", "retries_exhausted"):
                job.state = "pending"
                job.lease_owner = job.lease_until = None
                job.available_at = now(session)
        else:
            verify(session, job, lease)
            job.available_at = now(session) + timedelta(
                seconds=min(30, settings.worker_retry_seconds * job.attempts)
            )
            job.state = "pending"
            job.lease_owner = job.lease_until = None


def enqueue(session, run_id):
    run = session.scalar(select(Run).where(Run.id == run_id).with_for_update())
    if run is None:
        raise LookupError("Run not found.")
    if stop_target(run) is not None or deadline_due(run, now(session)):
        raise LookupError("Run deadline or stop intent prevents scheduling.")
    if run.status != "queued":
        raise LookupError("Only queued runs can be scheduled for simulation.")
    session.execute(
        insert(ExecutionJob)
        .values(run_id=run_id)
        .on_conflict_do_nothing(constraint="uq_execution_jobs_run_id")
    )
    return session.scalar(select(ExecutionJob.id).where(ExecutionJob.run_id == run_id))


def discover_due(engine, batch_size=50):
    """Bounded indexed discovery. Lock jobs before runs; skip busy candidates."""
    with Session(engine) as session:
        clock = now(session)
        ids = session.scalars(
            select(Run.id)
            .where(
                Run.expires_at.is_not(None),
                Run.expires_at <= clock,
                Run.cancellation_requested_at.is_(None),
                Run.expiration_requested_at.is_(None),
                Run.status.in_(["queued", "preparing", "starting", "running"]),
            )
            .order_by(Run.expires_at, Run.id)
            .limit(batch_size)
        ).all()
    processed = 0
    for run_id in ids:
        with Session(engine) as session, session.begin():
            session.execute(text("SET LOCAL lock_timeout = '1000ms'"))
            job = session.scalar(
                select(ExecutionJob)
                .where(ExecutionJob.run_id == run_id)
                .with_for_update(skip_locked=True)
            )
            if (
                job is None
                and session.scalar(
                    select(ExecutionJob.id).where(ExecutionJob.run_id == run_id)
                )
                is not None
            ):
                continue
            run = session.scalar(
                select(Run).where(Run.id == run_id).with_for_update(skip_locked=True)
            )
            if run is None:
                continue
            if (
                job is None
                and session.scalar(
                    select(ExecutionJob.id).where(ExecutionJob.run_id == run_id)
                )
                is not None
            ):
                continue  # enqueue won; preserve lock order and retry next sweep
            reconcile_stop(session, job, run)
            processed += 1
    return processed
