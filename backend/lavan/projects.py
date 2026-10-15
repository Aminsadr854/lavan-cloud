"""Authenticated project creation and owner-scoped retrieval only."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from lavan.auth import authenticated_user, get_session
from lavan.models import Project, User

router = APIRouter(prefix="/v1/projects")


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(strict=True, min_length=1, max_length=100)

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value):
        return value.strip() if isinstance(value, str) else value


class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    created_at: datetime
    updated_at: datetime


@router.post("", status_code=201, response_model=ProjectResponse)
def create_project(
    payload: ProjectCreate,
    response: Response,
    user: Annotated[User, Depends(authenticated_user)],
    session: Annotated[Session, Depends(get_session)],
):
    project = Project(name=payload.name, owner_id=user.id)
    session.add(project)
    session.commit()
    session.refresh(project)
    response.headers["Location"] = f"/v1/projects/{project.id}"
    return ProjectResponse.model_validate(project)


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(
    project_id: UUID,
    user: Annotated[User, Depends(authenticated_user)],
    session: Annotated[Session, Depends(get_session)],
):
    project = session.scalar(
        select(Project).where(Project.id == project_id, Project.owner_id == user.id)
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return ProjectResponse.model_validate(project)
