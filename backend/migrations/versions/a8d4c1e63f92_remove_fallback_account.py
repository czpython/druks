"""Remove the fallback account setting.

Revision ID: a8d4c1e63f92
Revises: f7c3b0d52e81
"""

from alembic import op

revision = "a8d4c1e63f92"
down_revision = "f7c3b0d52e81"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("settings", "fallback_account_id")


def downgrade() -> None:
    raise NotImplementedError("Account ownership migration is forward-only.")
