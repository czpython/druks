"""A notification keeps where Slack posted its message, so Druks can close the message
when its question ends."""

import sqlalchemy as sa
from alembic import op

revision = "c3f9a1d27b64"
down_revision = "8d4b1e6f2a93"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("notifications", sa.Column("message_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("notifications", "message_id")
