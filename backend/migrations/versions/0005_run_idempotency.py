"""Durable scoped retry keys; existing runs need no backfill."""

import sqlalchemy as sa
from alembic import op

revision = "0005_run_idempotency"
down_revision = "0004_runs"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "run_idempotency",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("key", sa.String(128, collation="C"), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint(
            "user_id", "operation", "key", name="pk_run_idempotency_scope"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="RESTRICT",
            name="fk_run_idempotency_user_id_users",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            ondelete="RESTRICT",
            name="fk_run_idempotency_run_id_runs",
        ),
        sa.UniqueConstraint("run_id", name="uq_run_idempotency_run_id"),
    )


def downgrade():
    op.drop_table("run_idempotency")
