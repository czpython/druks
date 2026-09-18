from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d3a7f1c9e5b2"
down_revision: str | Sequence[str] | None = "c7e2f4a9b1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sandbox_identities", sa.Column("account_id", sa.String(), nullable=True))
    op.execute(
        sa.text(
            "UPDATE sandbox_identities AS identity "
            "SET account_id = run.account_id "
            "FROM durable_runs AS run WHERE run.id = identity.run_id"
        )
    )
    op.alter_column("sandbox_identities", "account_id", nullable=False)
    op.alter_column("sandbox_identities", "run_id", nullable=True)
    op.create_foreign_key(
        "sandbox_identities_account_id_fkey",
        "sandbox_identities",
        "accounts",
        ["account_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_table(
        "chat_conversations",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("session_file", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["session_file"], ["files.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "chat_conversations_account_idx",
        "chat_conversations",
        ["account_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("body", sa.String(), nullable=False),
        sa.Column("reply_to", sa.String(), nullable=True),
        sa.Column("state", sa.String(), nullable=True),
        sa.Column("tool_calls", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["conversation_id"], ["chat_conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reply_to"], ["chat_messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "chat_messages_conversation_idx",
        "chat_messages",
        ["conversation_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("chat_messages_conversation_idx", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index("chat_conversations_account_idx", table_name="chat_conversations")
    op.drop_table("chat_conversations")
    op.drop_constraint(
        "sandbox_identities_account_id_fkey", "sandbox_identities", type_="foreignkey"
    )
    op.execute(sa.text("DELETE FROM sandbox_identities WHERE run_id IS NULL"))
    op.alter_column("sandbox_identities", "run_id", nullable=False)
    op.drop_column("sandbox_identities", "account_id")
