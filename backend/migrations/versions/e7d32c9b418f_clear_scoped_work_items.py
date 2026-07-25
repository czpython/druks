"""clear scoped work items

Revision ID: e7d32c9b418f
Revises: 1ba6e314b314
Create Date: 2026-07-25 18:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7d32c9b418f"
down_revision: str | Sequence[str] | None = "1ba6e314b314"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE work_items SET status = NULL WHERE status = 'scoped'")


def downgrade() -> None:
    pass
