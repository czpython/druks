"""Add chat reminders.

Revision ID: cb171bc569c7
Revises: 7c2d9a41e5b3
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cb171bc569c7"
down_revision: str | Sequence[str] | None = "7c2d9a41e5b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_reminders",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("body", sa.String(), nullable=False),
        sa.Column("cron", sa.String(), server_default=sa.text("''"), nullable=False),
        sa.Column("next_send_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["chat_conversations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("chat_reminders_account_idx", "chat_reminders", ["account_id", "created_at"])
    op.create_index("chat_reminders_due_idx", "chat_reminders", ["next_send_at"])


def downgrade() -> None:
    op.drop_index("chat_reminders_due_idx", table_name="chat_reminders")
    op.drop_index("chat_reminders_account_idx", table_name="chat_reminders")
    op.drop_table("chat_reminders")
