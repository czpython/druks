"""Key chat conversations by thread."""

import sqlalchemy as sa
from alembic import op

revision = "6d69d94c96e1"
down_revision = "b3d91f4a7c25"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_conversations",
        sa.Column("thread_id", sa.String(), nullable=False, server_default=""),
    )
    op.drop_index("chat_conversations_user_idx", table_name="chat_conversations")
    op.create_index(
        "chat_conversations_user_idx",
        "chat_conversations",
        ["connection_id", "account_id", "user_id", "thread_id"],
        unique=True,
        postgresql_where=sa.text("connection_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("chat_conversations_user_idx", table_name="chat_conversations")
    op.create_index(
        "chat_conversations_user_idx",
        "chat_conversations",
        ["connection_id", "account_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("connection_id IS NOT NULL"),
    )
    op.drop_column("chat_conversations", "thread_id")
