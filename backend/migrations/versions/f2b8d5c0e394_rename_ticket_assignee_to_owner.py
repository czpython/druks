"""Rename ticket assignee_id to owner_id.

Revision ID: f2b8d5c0e394
Revises: e1a7c4b9d283
Create Date: 2026-09-10
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f2b8d5c0e394"
down_revision: str | Sequence[str] | None = "e1a7c4b9d283"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("issues_tickets", "assignee_id", new_column_name="owner_id")


def downgrade() -> None:
    raise NotImplementedError("Owner is the name of this column.")
