"""Private ZIP artifact metadata."""

import sqlalchemy as sa
from alembic import op

revision = "0003_artifacts"
down_revision = "0002_api_tokens"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("storage_key", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("file_count", sa.BigInteger(), nullable=False),
        sa.Column("declared_uncompressed_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_artifacts"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_artifacts_project_id_projects",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("storage_key", name="uq_artifacts_storage_key"),
        *(
            sa.CheckConstraint(
                f"{col} >= 0", name=op.f(f"ck_artifacts_{col}_nonnegative")
            )
            for col in ("size_bytes", "file_count", "declared_uncompressed_bytes")
        ),
    )
    op.create_index("ix_artifacts_project_id", "artifacts", ["project_id"])


def downgrade():
    op.drop_index("ix_artifacts_project_id", table_name="artifacts")
    op.drop_table("artifacts")
