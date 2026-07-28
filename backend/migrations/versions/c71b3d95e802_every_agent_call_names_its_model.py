"""every agent call names its model

Revision ID: c71b3d95e802
Revises: a4e1f60c2b95
Create Date: 2026-07-28 19:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c71b3d95e802"
down_revision: str | Sequence[str] | None = "a4e1f60c2b95"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # No backfill, for the reason the agent column had none: the model is resolved
    # before the harness is chosen, so a null would name a writer nobody knows about
    # and a guessed model would land in the cost breakdown.
    op.alter_column("agent_calls", "model", existing_type=sa.String(), nullable=False)


def downgrade() -> None:
    op.alter_column("agent_calls", "model", existing_type=sa.String(), nullable=True)
