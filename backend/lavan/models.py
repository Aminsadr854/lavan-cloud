"""Minimal users, projects, and opaque API-token persistence."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "pk": "pk_%(table_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "ix": "ix_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
        }
    )


def utc_now() -> datetime:
    return datetime.now(UTC)


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (CheckConstraint("name ~ '[^[:space:]]'", name="name_nonblank"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = tuple(
        CheckConstraint(f"{column} >= 0", name=f"{column}_nonnegative")
        for column in ("size_bytes", "file_count", "declared_uncompressed_bytes")
    ) + (UniqueConstraint("id", "project_id", name="uq_artifacts_id_project_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    storage_key: Mapped[str] = mapped_column(String(64), unique=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    file_count: Mapped[int] = mapped_column(BigInteger)
    declared_uncompressed_bytes: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["artifact_id", "project_id"],
            ["artifacts.id", "artifacts.project_id"],
            name="fk_runs_artifact_project",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('queued','preparing','starting','running',"
            "'completed','failed')",
            name="status_supported",
        ),
        CheckConstraint(
            "runtime_template = 'python-fastapi-v1'", name="runtime_template_supported"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    artifact_id: Mapped[UUID] = mapped_column()
    status: Mapped[str] = mapped_column(
        String(16), default="queued", server_default="queued"
    )
    runtime_template: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    error_code: Mapped[str | None] = mapped_column(String(32))


class RunIdempotency(Base):
    __tablename__ = "run_idempotency"
    __table_args__ = (
        PrimaryKeyConstraint(
            "user_id", "operation", "key", name="pk_run_idempotency_scope"
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True
    )
    operation: Mapped[str] = mapped_column(String(32), primary_key=True)
    key: Mapped[str] = mapped_column(String(128, collation="C"), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"), unique=True
    )

    creation_response: Mapped[dict] = mapped_column(JSONB)


class ExecutionJob(Base):
    __tablename__ = "execution_jobs"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending','leased','completed','failed')", name="state_supported"
        ),
        CheckConstraint(
            "attempts >= 0 AND fence >= 0 AND max_attempts BETWEEN 1 AND 10",
            name="attempt_bounds",
        ),
        CheckConstraint(
            "(state = 'leased' AND lease_owner IS NOT NULL "
            "AND lease_until IS NOT NULL) OR (state <> 'leased' "
            "AND lease_owner IS NULL AND lease_until IS NULL)",
            name="lease_consistency",
        ),
        CheckConstraint(
            "scenario IS NULL OR scenario IN "
            "('success','startup_failure','slow_startup')",
            name="scenario_supported",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"), unique=True
    )
    state: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending"
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    fence: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    lease_owner: Mapped[UUID | None] = mapped_column()
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(32))
    scenario: Mapped[str | None] = mapped_column(String(32))
    startup_seconds: Mapped[float | None] = mapped_column(Float)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_run_events_run_sequence"),
        CheckConstraint("sequence > 0", name="sequence_positive"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="RESTRICT"))
    sequence: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
