"""Pin chat conversations."""

import sqlalchemy as sa
from alembic import op

revision = "32e4344e7ecf"
down_revision = "e5b8c2d4f7a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_conversations",
        sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("chat_conversations", "is_pinned")
