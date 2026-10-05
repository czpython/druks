"""Rename ticket owner to assignee

Revision ID: fc24ee008853
Revises: a597dd9055b9
Create Date: 2026-09-13 10:24:46.279927

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "fc24ee008853"
down_revision: str | Sequence[str] | None = "a597dd9055b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column("tickets", "owner_id", new_column_name="assignee_id")
    op.execute(
        "ALTER TABLE tickets RENAME CONSTRAINT tickets_owner_id_fkey TO tickets_assignee_id_fkey"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        "ALTER TABLE tickets RENAME CONSTRAINT tickets_assignee_id_fkey TO tickets_owner_id_fkey"
    )
    op.alter_column("tickets", "assignee_id", new_column_name="owner_id")
