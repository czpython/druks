"""Record MCP grant identity status and unreported scopes

Revision ID: a597dd9055b9
Revises: b1f4c07d9e52
Create Date: 2026-09-11 16:21:25.545227

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a597dd9055b9"
down_revision: str | Sequence[str] | None = "b1f4c07d9e52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("vault", sa.Column("identity_status", sa.String(), nullable=True))
    op.alter_column("vault", "scopes", existing_type=sa.JSON(), nullable=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("UPDATE vault SET scopes = '[]'::jsonb WHERE scopes IS NULL")
    op.alter_column("vault", "scopes", existing_type=sa.JSON(), nullable=False)
    op.drop_column("vault", "identity_status")
