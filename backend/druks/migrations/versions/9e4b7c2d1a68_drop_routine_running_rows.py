"""Drop routine running rows: Activity no longer records a run's start.

Revision ID: 9e4b7c2d1a68
Revises: 3b9e7a1f5c24
Create Date: 2026-10-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9e4b7c2d1a68"
down_revision: str | Sequence[str] | None = "3b9e7a1f5c24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A gate reply keeps its running row: it carries the reply as result.
    op.execute(
        sa.text("DELETE FROM events WHERE type = 'workflow.running' AND NOT payload ? 'result'")
    )


def downgrade() -> None:
    pass
