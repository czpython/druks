"""agent calls carry a failure code

Revision ID: b9e4d21c7a03
Revises: c2f6a9d8137e
Create Date: 2026-07-30 21:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b9e4d21c7a03"
down_revision: str | Sequence[str] | None = "c2f6a9d8137e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # No backfill: NULL already reads as unclassified.
    op.add_column("agent_calls", sa.Column("failure_code", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_calls", "failure_code")
