"""Add the include apps setting.

Revision ID: 3b9e7a1f5c24
Revises: d4e7a1c90b2f
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3b9e7a1f5c24"
down_revision: str | Sequence[str] | None = "d4e7a1c90b2f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "settings",
        sa.Column("include_apps", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("settings", "include_apps")
