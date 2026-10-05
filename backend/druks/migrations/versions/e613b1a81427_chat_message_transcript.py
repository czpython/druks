"""Keep the words of a chat message's voice note.

Revision ID: e613b1a81427
Revises: 2f9d7f55982a
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e613b1a81427"
down_revision: str | Sequence[str] | None = "2f9d7f55982a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chat_messages",
        sa.Column("transcript", sa.String(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("chat_messages", "transcript")
