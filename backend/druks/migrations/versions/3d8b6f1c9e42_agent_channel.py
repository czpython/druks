"""Give an agent a channel.

Revision ID: 3d8b6f1c9e42
Revises: 7a4c1e9b3d26
"""

import sqlalchemy as sa
from alembic import op

revision: str = "3d8b6f1c9e42"
down_revision: str | None = "7a4c1e9b3d26"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("channel_id", sa.String(), nullable=True))
    op.create_unique_constraint("agents_channel_id_key", "agents", ["channel_id"])


def downgrade() -> None:
    op.drop_column("agents", "channel_id")
