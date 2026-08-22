"""connections carry identity

Revision ID: c4f8a2d97e15
Revises: a7c2e9f14b38
Create Date: 2026-08-20 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c4f8a2d97e15"
down_revision: str | Sequence[str] | None = "a7c2e9f14b38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "oauth_connections",
        sa.Column("identity", postgresql.JSONB(), server_default="{}", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("oauth_connections", "identity")
