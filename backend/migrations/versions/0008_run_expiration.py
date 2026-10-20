"""PostgreSQL deadlines and shared durable stop arbitration; no legacy backfill."""

import sqlalchemy as sa
from alembic import op

revision = "0008_run_expiration"
down_revision = "0007_run_cancellation"
branch_labels = None
depends_on = None


def state_constraints(expiration):
    run_states = (
        "'queued','preparing','starting','running','completed','failed','stopped'"
    )
    job_states = "'pending','leased','completed','failed','stopped'"
    if expiration:
        run_states += ",'expired'"
        job_states += ",'expired'"
    for table, column, states in (
        ("runs", "status", run_states),
        ("execution_jobs", "state", job_states),
    ):
        name = op.f(f"ck_{table}_{column}_supported")
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, f"{column} IN ({states})")


def upgrade():
    op.add_column(
        "runs", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "runs",
        sa.Column("expiration_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    state_constraints(True)
    op.create_check_constraint(
        op.f("ck_runs_one_stop_reason"),
        "runs",
        "cancellation_requested_at IS NULL OR expiration_requested_at IS NULL",
    )
    op.create_check_constraint(
        op.f("ck_runs_positive_lifetime"),
        "runs",
        "expires_at IS NULL OR expires_at > created_at",
    )
    op.create_index(
        "ix_runs_expiration_due",
        "runs",
        ["expires_at", "id"],
        postgresql_where=sa.text(
            "expires_at IS NOT NULL AND cancellation_requested_at IS NULL "
            "AND expiration_requested_at IS NULL "
            "AND status IN ('queued','preparing','starting','running')"
        ),
    )
    # Validate at transaction end as well as the normal conditional UPDATE. An
    # outcome inspected/written before expiry must not commit after the deadline.
    op.execute("""
    CREATE FUNCTION lavan_check_run_deadline() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE current_run runs%ROWTYPE;
    BEGIN
      SELECT * INTO current_run FROM runs WHERE id = NEW.id;
      IF current_run.status IN ('preparing','starting','running','completed','failed')
         AND current_run.expires_at IS NOT NULL
         AND clock_timestamp() >= current_run.expires_at THEN
        RAISE EXCEPTION 'Run deadline reached' USING
          ERRCODE = '23514', CONSTRAINT = 'ck_runs_deadline_commit';
      END IF;
      RETURN NULL;
    END $$;
    CREATE CONSTRAINT TRIGGER ck_runs_deadline_commit
      AFTER UPDATE OF status ON runs DEFERRABLE INITIALLY DEFERRED
      FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM NEW.status)
      EXECUTE FUNCTION lavan_check_run_deadline();
    """)


def downgrade():
    if op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM runs WHERE expires_at IS NOT NULL "
            "OR expiration_requested_at IS NOT NULL OR status='expired'"
        )
    ):
        raise RuntimeError("Cannot discard expiration history during downgrade")
    op.execute("DROP TRIGGER ck_runs_deadline_commit ON runs")
    op.execute("DROP FUNCTION lavan_check_run_deadline()")
    op.drop_index("ix_runs_expiration_due", table_name="runs")
    op.drop_constraint(op.f("ck_runs_positive_lifetime"), "runs", type_="check")
    op.drop_constraint(op.f("ck_runs_one_stop_reason"), "runs", type_="check")
    state_constraints(False)
    op.drop_column("runs", "expiration_requested_at")
    op.drop_column("runs", "expires_at")
