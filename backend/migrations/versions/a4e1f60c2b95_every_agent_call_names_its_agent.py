"""every agent call names its agent

Revision ID: a4e1f60c2b95
Revises: f2c8b81a9d4e
Create Date: 2026-07-28 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4e1f60c2b95"
down_revision: str | Sequence[str] | None = "f2c8b81a9d4e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # No backfill: a call is written from the registered agent's own id, so a null
    # here would mean a writer nobody knows about. Inventing an attribution would
    # put it in the cost table — let the constraint find it instead.
    op.alter_column("agent_calls", "agent", existing_type=sa.String(), nullable=False)


def downgrade() -> None:
    op.alter_column("agent_calls", "agent", existing_type=sa.String(), nullable=True)
