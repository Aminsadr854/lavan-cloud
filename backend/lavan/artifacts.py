"""Private streamed artifacts; no extraction or payload execution."""

import asyncio
import hashlib
import time
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from lavan.auth import authenticated_user
from lavan.models import Artifact, Project
from lavan.storage import UploadFile
from lavan.zip_validation import InvalidZip, validate_zip

router = APIRouter(prefix="/v1/projects/{project_id}/artifacts")


class ArtifactResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    project_id: UUID
    size_bytes: int
    sha256: str
    file_count: int
    declared_uncompressed_bytes: int
    created_at: datetime


def owned_project(
    project_id: UUID,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
):
    # Reuse authentication with a short session that is CLOSED before streaming.
    try:
        with Session(request.app.state.auth_engine) as session:
            user = authenticated_user(session, authorization)
            owner_id = user.id
            if (
                session.scalar(
                    select(Project.id).where(
                        Project.id == project_id, Project.owner_id == owner_id
                    )
                )
                is None
            ):
                raise HTTPException(404, "Not found")
            return owner_id
    except SQLAlchemyError:
        raise HTTPException(503, "Service unavailable") from None


async def blocking(function, *args):
    # Never abandon filesystem/validation/DB work on task cancellation. Drain it
    # before cleanup; capacity remains occupied until the actual work finishes.
    task = asyncio.create_task(run_in_threadpool(function, *args))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result


def persist(engine, upload, project_id, owner_id, size, digest, counts):
    attempted = False
    try:
        with Session(engine, expire_on_commit=False) as session:
            if (
                session.scalar(
                    select(Project.id).where(
                        Project.id == project_id, Project.owner_id == owner_id
                    )
                )
                is None
            ):
                raise HTTPException(404, "Not found")
            artifact = Artifact(
                id=upload.id,
                project_id=project_id,
                storage_key=upload.key,
                size_bytes=size,
                sha256=digest,
                file_count=counts[0],
                declared_uncompressed_bytes=counts[1],
            )
            session.add(artifact)
            attempted = True
            session.commit()
            upload.persisted = True
            return ArtifactResponse.model_validate(artifact)
    except SQLAlchemyError:
        if attempted:
            # A lost COMMIT acknowledgement can mean the row already exists.
            # Never remove a possibly referenced file while the DB is unreachable.
            upload.keep_published = True
            try:
                with Session(engine) as verification:
                    upload.keep_published = (
                        verification.get(Artifact, upload.id) is not None
                    )
            except SQLAlchemyError:
                pass
        raise


@router.post("", status_code=201, response_model=ArtifactResponse)
async def upload_artifact(
    project_id: UUID,
    request: Request,
    response: Response,
    owner_id: Annotated[UUID, Depends(owned_project)],
):
    settings = request.app.state.settings
    if (
        request.headers.get("content-type", "").split(";")[0].strip().lower()
        != "application/zip"
    ):
        raise HTTPException(415, "Content-Type must be application/zip")
    if request.app.state.active_uploads >= settings.upload_max_active:
        raise HTTPException(503, "Upload capacity exhausted")
    request.app.state.active_uploads += 1  # No await between check and acquisition.
    upload = UploadFile(settings.artifact_storage_dir)
    try:
        deadline = time.monotonic() + settings.upload_max_seconds
        await blocking(upload.open)
        size = 0
        digest = hashlib.sha256()
        stream = request.stream().__aiter__()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            try:
                chunk = await asyncio.wait_for(anext(stream), remaining)
            except StopAsyncIteration:
                break
            size += len(chunk)
            if size > settings.zip_max_compressed_bytes:
                raise HTTPException(413, "Upload too large")
            for offset in range(0, len(chunk), 65536):
                if time.monotonic() >= deadline:
                    raise TimeoutError
                piece = chunk[offset : offset + 65536]

                # Hashing/writes run off the event loop, in at most 64-KiB pieces.
                def write_piece(data=piece):
                    digest.update(data)
                    upload.write(data)

                await blocking(write_piece)
        await blocking(upload.finish)
        counts = await blocking(validate_zip, upload.temporary, settings)
        if time.monotonic() >= deadline:
            raise TimeoutError
        await blocking(upload.publish)
        result = await blocking(
            persist,
            request.app.state.auth_engine,
            upload,
            project_id,
            owner_id,
            size,
            digest.hexdigest(),
            counts,
        )
        response.headers["Location"] = (
            f"/v1/projects/{project_id}/artifacts/{upload.id}"
        )
        return result
    except TimeoutError:
        raise HTTPException(408, "Upload timed out") from None
    except ClientDisconnect:
        raise HTTPException(400, "Upload interrupted") from None
    except InvalidZip:
        raise HTTPException(422, "Invalid or unsupported ZIP") from None
    except (OSError, SQLAlchemyError):
        raise HTTPException(503, "Service unavailable") from None
    finally:
        try:
            await blocking(upload.cleanup)
        except OSError:
            raise HTTPException(503, "Service unavailable") from None
        finally:
            request.app.state.active_uploads -= 1


@router.get("/{artifact_id}", response_model=ArtifactResponse)
def artifact_metadata(
    project_id: UUID,
    artifact_id: UUID,
    request: Request,
    owner_id: Annotated[UUID, Depends(owned_project)],
):
    try:
        with Session(request.app.state.auth_engine) as session:
            artifact = session.scalar(
                select(Artifact).where(
                    Artifact.id == artifact_id, Artifact.project_id == project_id
                )
            )
            if artifact is None:
                raise HTTPException(404, "Not found")
            return ArtifactResponse.model_validate(artifact)
    except SQLAlchemyError:
        raise HTTPException(503, "Service unavailable") from None
