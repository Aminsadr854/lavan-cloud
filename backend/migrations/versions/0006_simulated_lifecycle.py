"""Fenced jobs, transition events, and immutable creation responses."""

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006_simulated_lifecycle"
down_revision = "0005_run_idempotency"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    # Abort rather than manufacture a queued snapshot from mutable state.
    if connection.scalar(sa.text("SELECT count(*) FROM runs WHERE status <> 'queued'")):
        raise RuntimeError("Snapshot backfill requires all existing runs to be queued")
    op.add_column(
        "run_idempotency", sa.Column("creation_response", JSONB(), nullable=True)
    )
    rows = connection.execute(
        sa.text(
            "SELECT i.user_id,i.operation,i.key,r.id,r.project_id,"
            "r.artifact_id,r.status,"
            "r.runtime_template,r.created_at FROM run_idempotency i "
            "JOIN runs r ON r.id=i.run_id"
        )
    ).mappings()
    for row in rows:
        snapshot = {
            k: str(row[k])
            for k in ("id", "project_id", "artifact_id", "status", "runtime_template")
        }
        snapshot["created_at"] = row["created_at"].isoformat().replace("+00:00", "Z")
        connection.execute(
            sa.text(
                "UPDATE run_idempotency SET creation_response=CAST(:snapshot AS jsonb) "
                "WHERE user_id=:user_id AND operation=:operation AND key=:key"
            ),
            {
                "snapshot": json.dumps(snapshot),
                "user_id": row["user_id"],
                "operation": row["operation"],
                "key": row["key"],
            },
        )
    op.alter_column("run_idempotency", "creation_response", nullable=False)
    op.add_column(
        "runs",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    connection.execute(sa.text("UPDATE runs SET updated_at=created_at"))
    op.add_column("runs", sa.Column("error_code", sa.String(32), nullable=True))
    op.drop_constraint(op.f("ck_runs_status_queued"), "runs", type_="check")
    op.create_check_constraint(
        op.f("ck_runs_status_supported"),
        "runs",
        "status IN ('queued','preparing','starting','running','completed','failed')",
    )
    op.create_table(
        "execution_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(16), server_default="pending", nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("fence", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("lease_owner", sa.Uuid(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(32), nullable=True),
        sa.Column("scenario", sa.String(32), nullable=True),
        sa.Column("startup_seconds", sa.Float(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_execution_jobs"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name="fk_execution_jobs_run_id_runs",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("run_id", name="uq_execution_jobs_run_id"),
        sa.CheckConstraint(
            "state IN ('pending','leased','completed','failed')",
            name=op.f("ck_execution_jobs_state_supported"),
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND fence >= 0 AND max_attempts BETWEEN 1 AND 10",
            name=op.f("ck_execution_jobs_attempt_bounds"),
        ),
        sa.CheckConstraint(
            "(state = 'leased' AND lease_owner IS NOT NULL "
            "AND lease_until IS NOT NULL) OR (state <> 'leased' "
            "AND lease_owner IS NULL AND lease_until IS NULL)",
            name=op.f("ck_execution_jobs_lease_consistency"),
        ),
        sa.CheckConstraint(
            "scenario IS NULL OR scenario IN "
            "('success','startup_failure','slow_startup')",
            name=op.f("ck_execution_jobs_scenario_supported"),
        ),
    )
    op.create_index(
        "ix_execution_jobs_available_at", "execution_jobs", ["available_at"]
    )
    op.create_table(
        "run_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_run_events"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name="fk_run_events_run_id_runs",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("run_id", "sequence", name="uq_run_events_run_sequence"),
        sa.CheckConstraint(
            "sequence > 0", name=op.f("ck_run_events_sequence_positive")
        ),
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT count(*) FROM runs WHERE status <> 'queued'")
    ):
        raise RuntimeError("Cannot downgrade lifecycle while nonqueued runs exist")
    op.drop_table("run_events")
    op.drop_index("ix_execution_jobs_available_at", table_name="execution_jobs")
    op.drop_table("execution_jobs")
    op.drop_constraint(op.f("ck_runs_status_supported"), "runs", type_="check")
    op.create_check_constraint(
        op.f("ck_runs_status_queued"), "runs", "status = 'queued'"
    )
    op.drop_column("runs", "error_code")
    op.drop_column("runs", "updated_at")
    op.drop_column("run_idempotency", "creation_response")
