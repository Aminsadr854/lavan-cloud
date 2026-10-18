"""Queued execution requests tied to an artifact in the same project."""

import sqlalchemy as sa
from alembic import op

revision = "0004_runs"
down_revision = "0003_artifacts"
branch_labels = None
depends_on = None


def upgrade():
    # PostgreSQL requires an explicit unique target for this composite FK,
    # despite artifacts.id already being globally unique.
    op.create_unique_constraint(
        "uq_artifacts_id_project_id", "artifacts", ["id", "project_id"]
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status", sa.String(16), server_default=sa.text("'queued'"), nullable=False
        ),
        sa.Column("runtime_template", sa.String(32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_runs"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_runs_project_id_projects",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id", "project_id"],
            ["artifacts.id", "artifacts.project_id"],
            name="fk_runs_artifact_project",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("status = 'queued'", name=op.f("ck_runs_status_queued")),
        sa.CheckConstraint(
            "runtime_template = 'python-fastapi-v1'",
            name=op.f("ck_runs_runtime_template_supported"),
        ),
    )
    op.create_index("ix_runs_project_id", "runs", ["project_id"])


def downgrade():
    op.drop_index("ix_runs_project_id", table_name="runs")
    op.drop_table("runs")
    op.drop_constraint("uq_artifacts_id_project_id", "artifacts", type_="unique")
