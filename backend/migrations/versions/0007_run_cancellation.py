"""Durable owner cancellation; creation snapshots remain unchanged."""

import sqlalchemy as sa
from alembic import op

revision = "0007_run_cancellation"
down_revision = "0006_simulated_lifecycle"
branch_labels = None
depends_on = None


def constraints(stopped):
    run_states = "'queued','preparing','starting','running','completed','failed'"
    job_states = "'pending','leased','completed','failed'"
    if stopped:
        run_states += ",'stopped'"
        job_states += ",'stopped'"
    for table, column, values in (
        ("runs", "status", run_states),
        ("execution_jobs", "state", job_states),
    ):
        name = op.f(f"ck_{table}_{column}_supported")
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, f"{column} IN ({values})")


def upgrade():
    op.add_column(
        "runs",
        sa.Column(
            "cancellation_requested_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    constraints(True)


def downgrade():
    if op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM runs WHERE cancellation_requested_at IS NOT NULL "
            "OR status='stopped'"
        )
    ):
        raise RuntimeError("Cannot discard cancellation history during downgrade")
    constraints(False)
    op.drop_column("runs", "cancellation_requested_at")
