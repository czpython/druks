"""Move leftover Todo tickets onto Backlog.

Revision ID: e1a7c4b9d283
Revises: d4a9e2b8c173
Create Date: 2026-09-10
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e1a7c4b9d283"
down_revision: str | Sequence[str] | None = "d4a9e2b8c173"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE issues_tickets SET status = 'backlog' WHERE status = 'todo'")


def downgrade() -> None:
    raise NotImplementedError("Todo is no longer a status.")
