"""Record a subscription's last successful token refresh

Revision ID: 9281b05a13da
Revises: fc24ee008853
Create Date: 2026-09-13 12:32:44.422268

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9281b05a13da"
down_revision: str | Sequence[str] | None = "fc24ee008853"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "vault", sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("vault", "last_refreshed_at")
