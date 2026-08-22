"""revoking is a state

Revision ID: b6d4f2a81c93
Revises: c4f8a2d97e15
Create Date: 2026-08-22 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b6d4f2a81c93"
down_revision: str | Sequence[str] | None = "c4f8a2d97e15"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "oauth_connections",
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "oauth_connections",
        sa.Column("revoked_reason", sa.String(), server_default="", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("oauth_connections", "revoked_reason")
    op.drop_column("oauth_connections", "revoked_at")
