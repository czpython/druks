"""harness models fetched

Revision ID: f0a1b2c3d4e5
Revises: e8f9a0b1c2d3
Create Date: 2026-07-18 17:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f0a1b2c3d4e5"
down_revision: str | Sequence[str] | None = "e8f9a0b1c2d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Provider-fetched picker models; null until the first successful fetch,
    # when the harness's shipped tuple serves the picker.
    op.add_column("harnesses", sa.Column("models_fetched", postgresql.JSONB(), nullable=True))
    op.add_column(
        "harnesses", sa.Column("models_fetched_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("harnesses", "models_fetched_at")
    op.drop_column("harnesses", "models_fetched")
