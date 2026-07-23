"""personal access tokens

Revision ID: 0ec9db44973e
Revises: 40cfd4c2aeee
Create Date: 2026-07-19 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0ec9db44973e"
down_revision: str | Sequence[str] | None = "40cfd4c2aeee"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "personal_access_tokens",
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("token_prefix", sa.String(length=12), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "personal_access_tokens_account_idx",
        "personal_access_tokens",
        ["account_id", "revoked_at", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_personal_access_tokens_token_prefix"),
        "personal_access_tokens",
        ["token_prefix"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_personal_access_tokens_token_prefix"), table_name="personal_access_tokens"
    )
    op.drop_index("personal_access_tokens_account_idx", table_name="personal_access_tokens")
    op.drop_table("personal_access_tokens")
