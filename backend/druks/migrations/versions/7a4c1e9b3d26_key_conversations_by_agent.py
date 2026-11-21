"""Key a person's channel conversations by agent.

Revision ID: 7a4c1e9b3d26
Revises: 4b7e2d9c1a58
"""

import sqlalchemy as sa
from alembic import op

revision: str = "7a4c1e9b3d26"
down_revision: str | None = "4b7e2d9c1a58"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_index("chat_conversations_user_idx", table_name="chat_conversations")
    op.create_index(
        "chat_conversations_user_idx",
        "chat_conversations",
        ["connection_id", "account_id", "user_id", "thread_id", "agent_id"],
        unique=True,
        postgresql_where=sa.text("connection_id IS NOT NULL"),
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    op.drop_index("chat_conversations_user_idx", table_name="chat_conversations")
    op.create_index(
        "chat_conversations_user_idx",
        "chat_conversations",
        ["connection_id", "account_id", "user_id", "thread_id"],
        unique=True,
        postgresql_where=sa.text("connection_id IS NOT NULL"),
    )
