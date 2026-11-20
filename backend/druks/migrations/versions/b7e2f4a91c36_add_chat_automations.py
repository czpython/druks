"""Add the automations that write to a Bot on a channel.

Revision ID: b7e2f4a91c36
Revises: 1e4a9c2b7d3f
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7e2f4a91c36"
down_revision: str | None = "1e4a9c2b7d3f"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "chat_automations",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("connection_id", sa.String(), nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("is_accepted", sa.Boolean(), nullable=False),
        sa.Column("first_message", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["connection_id"], ["vault.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "chat_automations_source_idx",
        "chat_automations",
        ["connection_id", "source_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("chat_automations_source_idx", table_name="chat_automations")
    op.drop_table("chat_automations")
