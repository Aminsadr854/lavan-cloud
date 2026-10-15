"""Add opaque API-token digests and revocation."""

import sqlalchemy as sa
from alembic import op

revision = "0002_api_tokens"
down_revision = "0001_users_projects"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_api_tokens"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_api_tokens_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("digest", name="uq_api_tokens_digest"),
    )
    op.create_index("ix_api_tokens_user_id", "api_tokens", ["user_id"])


def downgrade():
    op.drop_index("ix_api_tokens_user_id", table_name="api_tokens")
    op.drop_table("api_tokens")
