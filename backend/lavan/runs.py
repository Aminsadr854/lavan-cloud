"""Owned execution requests with stable creation snapshots and durable jobs."""

import hashlib
import json
import re
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from lavan.auth import authenticated_user, get_session
from lavan.jobs import CancellationBusy, request_cancellation
from lavan.models import Artifact, ExecutionJob, Project, Run, RunIdempotency, User

router = APIRouter(prefix="/v1/projects/{project_id}/runs")


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_id: UUID
    runtime_template: Literal["python-fastapi-v1"]


class RunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    project_id: UUID
    artifact_id: UUID
    status: Literal[
        "queued", "preparing", "starting", "running", "completed", "failed", "stopped"
    ]
    runtime_template: Literal["python-fastapi-v1"]
    created_at: datetime


class RunCurrentResponse(RunResponse):
    cancellation_requested_at: datetime | None


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


OPERATION = "run.create"


def request_fingerprint(
    project_id: UUID, artifact_id: UUID, runtime_template: str
) -> str:
    canonical = json.dumps(
        {
            "version": 1,
            "project_id": str(project_id),
            "artifact_id": str(artifact_id),
            "runtime_template": runtime_template,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def authorize_artifact(session, user_id, project_id, artifact_id):
    if (
        session.scalar(
            select(Artifact.id)
            .join(Project, Artifact.project_id == Project.id)
            .where(
                Artifact.id == artifact_id,
                Project.id == project_id,
                Project.owner_id == user_id,
            )
        )
        is None
    ):
        raise HTTPException(404, "Not found")


@router.post("", status_code=201, response_model=RunResponse)
def create_run(
    project_id: UUID,
    payload: RunCreate,
    request: Request,
    response: Response,
    user: Annotated[User, Depends(authenticated_user)],
    session: Annotated[Session, Depends(get_session)],
):
    keys = request.headers.getlist("idempotency-key")
    if len(keys) != 1 or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", keys[0]) is None:
        raise HTTPException(422, "Invalid Idempotency-Key")
    user_id = user.id
    identity = (user_id, OPERATION, keys[0])
    fingerprint = request_fingerprint(
        project_id, payload.artifact_id, payload.runtime_template
    )
    authorize_artifact(session, user_id, project_id, payload.artifact_id)
    record = session.get(RunIdempotency, identity)
    if record is None:
        # Bound concurrent-writer waits as well as driver connect timeouts.
        session.execute(text("SET LOCAL lock_timeout = '1500ms'"))
        run = Run(
            project_id=project_id,
            artifact_id=payload.artifact_id,
            runtime_template=payload.runtime_template,
        )
        session.add(run)
        session.flush()
        snapshot = RunResponse.model_validate(run).model_dump(mode="json")
        session.add(ExecutionJob(run_id=run.id))
        inserted = session.execute(
            insert(RunIdempotency)
            .values(
                user_id=user_id,
                operation=OPERATION,
                key=keys[0],
                fingerprint=fingerprint,
                run_id=run.id,
                creation_response=snapshot,
            )
            .on_conflict_do_nothing(constraint="pk_run_idempotency_scope")
            .returning(RunIdempotency.run_id)
        ).scalar_one_or_none()
        if inserted is None:
            # Discard the losing run, then read the committed winner in a fresh
            # transaction. Unrelated constraints/errors are never suppressed.
            session.rollback()
            authorize_artifact(session, user_id, project_id, payload.artifact_id)
        else:
            session.commit()
        record = session.get(RunIdempotency, identity)
        if record is None:
            raise HTTPException(503, "Service unavailable")
    if record.fingerprint != fingerprint:
        raise HTTPException(409, "Idempotency conflict")
    run = session.scalar(
        select(Run)
        .join(Project, Run.project_id == Project.id)
        .where(
            Run.id == record.run_id,
            Project.owner_id == user_id,
            Run.project_id == project_id,
            Run.artifact_id == payload.artifact_id,
        )
    )
    if run is None:
        raise HTTPException(404, "Not found")
    response.headers["Location"] = f"/v1/projects/{project_id}/runs/{run.id}"
    return RunResponse.model_validate(record.creation_response)


@router.get("/{run_id}", response_model=RunCurrentResponse)
def get_run(
    project_id: UUID,
    run_id: UUID,
    user: Annotated[User, Depends(authenticated_user)],
    session: Annotated[Session, Depends(get_session)],
):
    run = session.scalar(
        select(Run)
        .join(Project, Run.project_id == Project.id)
        .where(
            Run.id == run_id,
            Run.project_id == project_id,
            Project.owner_id == user.id,
        )
    )
    if run is None:
        raise HTTPException(404, "Not found")
    return RunCurrentResponse.model_validate(run)


@router.post(
    "/{run_id}/cancel",
    response_model=RunCurrentResponse,
    responses={
        202: {
            "model": RunCurrentResponse,
            "description": "Durable cancellation awaiting worker acknowledgement",
        }
    },
)
def cancel_run(
    project_id: UUID,
    run_id: UUID,
    response: Response,
    user: Annotated[User, Depends(authenticated_user)],
    session: Annotated[Session, Depends(get_session)],
    payload: Annotated[CancelRequest | None, Body()] = None,
):
    try:
        run, pending = request_cancellation(session, user.id, project_id, run_id)
    except LookupError:
        raise HTTPException(404, "Not found") from None
    except CancellationBusy:
        raise HTTPException(503, "Service unavailable") from None
    body = RunCurrentResponse.model_validate(run)
    session.commit()
    response.status_code = 202 if pending else 200
    if pending:
        response.headers["Location"] = f"/v1/projects/{project_id}/runs/{run_id}"
    return body
