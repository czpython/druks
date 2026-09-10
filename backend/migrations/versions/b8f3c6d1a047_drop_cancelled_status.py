"""Move leftover Cancelled tickets onto Done.

Revision ID: b8f3c6d1a047
Revises: a3c9e1f4b072
Create Date: 2026-09-10
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b8f3c6d1a047"
down_revision: str | Sequence[str] | None = "a3c9e1f4b072"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE issues_tickets SET status = 'done' WHERE status = 'cancelled'")


def downgrade() -> None:
    raise NotImplementedError("Cancelled is no longer a status.")
