"""Enforce one settings row for the installation.

Revision ID: b43924bf37db
Revises: 7dc609a2d51a
"""

import sqlalchemy as sa
from alembic import op

revision = "b43924bf37db"
down_revision = "7dc609a2d51a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("accounts", "timezone", existing_type=sa.String(), nullable=False)
    op.drop_column("settings", "account_id")
    op.drop_column("settings", "timezone")
    op.create_check_constraint("settings_singleton", "settings", "id = 1")


def downgrade() -> None:
    raise NotImplementedError("Installation settings separation is forward-only.")
