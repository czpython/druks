"""Add a conversation's agent.

Revision ID: 9c3e1f7a2b64
Revises: 57f5c42b7d19
"""

import sqlalchemy as sa
from alembic import op

revision: str = "9c3e1f7a2b64"
down_revision: str | None = "57f5c42b7d19"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("chat_conversations", sa.Column("agent_id", sa.String(), nullable=True))
    op.create_foreign_key(
        "chat_conversations_agent_id_fkey",
        "chat_conversations",
        "agents",
        ["agent_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("chat_conversations_agent_id_fkey", "chat_conversations", type_="foreignkey")
    op.drop_column("chat_conversations", "agent_id")
