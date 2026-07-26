"""feed events say workflow, not run

Revision ID: b5e91c2d7a34
Revises: 4c1f0ab7d2e9
Create Date: 2026-07-26 14:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b5e91c2d7a34"
down_revision: str | Sequence[str] | None = "4c1f0ab7d2e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The renamed state goes first: once it reads 'workflow.parked' the generic
    # rewrite below no longer matches it.
    op.execute("UPDATE events SET type = 'workflow.parked' WHERE type = 'run.pending_input'")
    op.execute(
        "UPDATE events SET type = 'workflow.' || substring(type from 5) WHERE type LIKE 'run.%'"
    )


def downgrade() -> None:
    op.execute("UPDATE events SET type = 'run.pending_input' WHERE type = 'workflow.parked'")
    op.execute(
        "UPDATE events SET type = 'run.' || substring(type from 10) WHERE type LIKE 'workflow.%'"
    )
