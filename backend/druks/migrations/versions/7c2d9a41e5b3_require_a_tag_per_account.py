"""Require a tag per account.

Revision ID: 7c2d9a41e5b3
Revises: 3b8e1f6c9d24
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7c2d9a41e5b3"
down_revision: str | Sequence[str] | None = "3b8e1f6c9d24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("is_tag_required", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("accounts", "is_tag_required")
