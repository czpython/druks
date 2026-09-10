"""Add account preference columns.

Revision ID: 74b53981122c
Revises: a3c9e7f1b5d2
"""

import sqlalchemy as sa
from alembic import op

revision = "74b53981122c"
down_revision = "a3c9e7f1b5d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("timezone", sa.String(), nullable=True))
    op.add_column("accounts", sa.Column("gate_park_destination_id", sa.String(), nullable=True))
    op.create_foreign_key(
        "accounts_gate_park_destination_id_fkey",
        "accounts",
        "notification_destinations",
        ["gate_park_destination_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    raise NotImplementedError("Account preference storage is forward-only.")
