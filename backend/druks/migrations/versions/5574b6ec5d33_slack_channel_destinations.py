"""A notification destination is a Slack channel that the bot posts to.

Revision ID: 5574b6ec5d33
Revises: 3b9e7a1f5c24
Create Date: 2026-10-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "5574b6ec5d33"
down_revision: str | Sequence[str] | None = "3b9e7a1f5c24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A webhook destination names no channel, so it goes. Its notifications reference
    # it with no ondelete and go first. Accounts and installation settings drop their
    # reference by SET NULL.
    op.execute(
        "DELETE FROM notifications WHERE destination_id IN "
        "(SELECT id FROM notification_destinations WHERE kind = 'slack_webhook')"
    )
    op.execute("DELETE FROM notification_destinations WHERE kind = 'slack_webhook'")
    op.alter_column("notification_destinations", "url", new_column_name="channel_id")


def downgrade() -> None:
    op.alter_column("notification_destinations", "channel_id", new_column_name="url")
