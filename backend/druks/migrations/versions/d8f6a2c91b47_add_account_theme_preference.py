"""Add an account-owned theme preference.

Revision ID: d8f6a2c91b47
Revises: 3b9e7a1f5c24
"""

import sqlalchemy as sa
from alembic import op

revision = "d8f6a2c91b47"
down_revision = "3b9e7a1f5c24"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("theme", sa.String(), server_default="system", nullable=False),
    )
    op.create_check_constraint(
        "accounts_theme_allowed", "accounts", "theme IN ('system', 'light', 'dark')"
    )


def downgrade() -> None:
    op.drop_constraint("accounts_theme_allowed", "accounts", type_="check")
    op.drop_column("accounts", "theme")
