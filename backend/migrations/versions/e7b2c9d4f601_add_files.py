"""Add files.

Revision ID: e7b2c9d4f601
Revises: a4b9d3e17c62
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7b2c9d4f601"
down_revision: str | Sequence[str] | None = "a4b9d3e17c62"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "files",
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("content_type", sa.String(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("app", sa.String(), nullable=False),
        sa.Column("origin_type", sa.String(), nullable=False),
        sa.Column("origin_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(), nullable=False),
        sa.CheckConstraint(
            "origin_type IN ('agent_call')",
            name="files_origin_type_check",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("files_deleted_at_idx", "files", ["deleted_at"], unique=False)


def downgrade() -> None:
    op.drop_index("files_deleted_at_idx", table_name="files")
    op.drop_table("files")
