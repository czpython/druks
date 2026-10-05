from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5b8c2d4f7a1"
down_revision: str | Sequence[str] | None = "6e9c2b4a8f10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_vault_one_per_audience", table_name="vault")
    op.create_index(
        "ix_vault_one_per_audience",
        "vault",
        ["kind", "audience", "account_id", "header"],
        unique=True,
        postgresql_where=sa.text("kind NOT IN ('oauth', 'session')"),
        postgresql_nulls_not_distinct=True,
    )
    op.add_column(
        "accounts",
        sa.Column("kind", sa.String(), server_default=sa.text("'operator'"), nullable=False),
    )
    op.add_column("chat_conversations", sa.Column("connection_id", sa.String(), nullable=True))
    op.add_column("chat_conversations", sa.Column("user_id", sa.String(), nullable=True))
    op.add_column(
        "chat_conversations",
        sa.Column("user_name", sa.String(), server_default=sa.text("''"), nullable=False),
    )
    op.add_column(
        "chat_conversations",
        sa.Column("user_phone", sa.String(), server_default=sa.text("''"), nullable=False),
    )
    op.create_foreign_key(
        "chat_conversations_connection_id_fkey",
        "chat_conversations",
        "vault",
        ["connection_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "chat_conversations_user_idx",
        "chat_conversations",
        ["connection_id", "account_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("connection_id IS NOT NULL"),
    )
    op.add_column(
        "chat_messages",
        sa.Column("is_internal", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("chat_messages", sa.Column("source_id", sa.String(), nullable=True))
    op.add_column("chat_messages", sa.Column("file", sa.String(), nullable=True))
    op.create_unique_constraint("chat_messages_source_id_key", "chat_messages", ["source_id"])
    op.create_foreign_key(
        "chat_messages_file_fkey", "chat_messages", "files", ["file"], ["id"], ondelete="RESTRICT"
    )
    op.add_column("durable_runs", sa.Column("conversation_id", sa.String(), nullable=True))
    op.create_foreign_key(
        "durable_runs_conversation_id_fkey",
        "durable_runs",
        "chat_conversations",
        ["conversation_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("durable_runs_conversation_id_fkey", "durable_runs", type_="foreignkey")
    op.drop_column("durable_runs", "conversation_id")
    op.drop_constraint("chat_messages_file_fkey", "chat_messages", type_="foreignkey")
    op.drop_constraint("chat_messages_source_id_key", "chat_messages", type_="unique")
    op.drop_column("chat_messages", "file")
    op.drop_column("chat_messages", "source_id")
    op.drop_column("chat_messages", "is_internal")
    op.drop_index("chat_conversations_user_idx", table_name="chat_conversations")
    op.drop_constraint(
        "chat_conversations_connection_id_fkey", "chat_conversations", type_="foreignkey"
    )
    op.drop_column("chat_conversations", "user_phone")
    op.drop_column("chat_conversations", "user_name")
    op.drop_column("chat_conversations", "user_id")
    op.drop_column("chat_conversations", "connection_id")
    op.drop_column("accounts", "kind")
    op.execute(sa.text("DELETE FROM vault WHERE kind = 'session'"))
    op.drop_index("ix_vault_one_per_audience", table_name="vault")
    op.create_index(
        "ix_vault_one_per_audience",
        "vault",
        ["kind", "audience", "account_id", "header"],
        unique=True,
        postgresql_where=sa.text("kind <> 'oauth'"),
        postgresql_nulls_not_distinct=True,
    )
