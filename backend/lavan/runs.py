"""Persist execution requests only. Nothing dispatches or executes these runs."""

import hashlib
import json
import re
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from lavan.auth import authenticated_user, get_session
from lavan.models import Artifact, Project, Run, RunIdempotency, User

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
    status: Literal["queued"]
    runtime_template: Literal["python-fastapi-v1"]
    created_at: datetime


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
        inserted = session.execute(
            insert(RunIdempotency)
            .values(
                user_id=user_id,
                operation=OPERATION,
                key=keys[0],
                fingerprint=fingerprint,
                run_id=run.id,
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
    return RunResponse.model_validate(run)


@router.get("/{run_id}", response_model=RunResponse)
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
    return RunResponse.model_validate(run)
