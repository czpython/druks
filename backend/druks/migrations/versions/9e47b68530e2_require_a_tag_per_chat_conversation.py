"""Require a tag per chat conversation.

Revision ID: 9e47b68530e2
Revises: e613b1a81427
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9e47b68530e2"
down_revision: str | Sequence[str] | None = "e613b1a81427"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chat_conversations",
        sa.Column("is_tag_required", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("chat_conversations", "is_tag_required")
