"""Persist execution requests only. Nothing dispatches or executes these runs."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from lavan.auth import authenticated_user, get_session
from lavan.models import Artifact, Project, Run, User

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


@router.post("", status_code=201, response_model=RunResponse)
def create_run(
    project_id: UUID,
    payload: RunCreate,
    response: Response,
    user: Annotated[User, Depends(authenticated_user)],
    session: Annotated[Session, Depends(get_session)],
):
    artifact = session.scalar(
        select(Artifact.id)
        .join(Project, Artifact.project_id == Project.id)
        .where(
            Artifact.id == payload.artifact_id,
            Project.id == project_id,
            Project.owner_id == user.id,
        )
    )
    if artifact is None:
        raise HTTPException(404, "Not found")
    run = Run(
        project_id=project_id,
        artifact_id=artifact,
        runtime_template=payload.runtime_template,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
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
