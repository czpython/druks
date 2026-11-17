"""An account names the Slack channel for its notices, and the installation has no default.

People's accounts copied the old installation default, so nobody chose that channel:
their value is cleared. The unattended account keeps its channel."""

import sqlalchemy as sa
from alembic import op

revision = "c4e8a1f7d2b9"
down_revision = "b9d643a10663"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("accounts_gate_park_destination_id_fkey", "accounts", type_="foreignkey")
    op.alter_column("accounts", "gate_park_destination_id", new_column_name="notice_channel_id")
    op.execute(
        "UPDATE accounts SET notice_channel_id = ("
        " SELECT channel_id FROM notification_destinations"
        " WHERE notification_destinations.id = accounts.notice_channel_id AND accounts.is_default"
        ")"
    )
    op.drop_column("settings", "gate_park_destination_id")


def downgrade() -> None:
    op.add_column("settings", sa.Column("gate_park_destination_id", sa.String(), nullable=True))
    op.create_foreign_key(
        "settings_gate_park_destination_id_fkey",
        "settings",
        "notification_destinations",
        ["gate_park_destination_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # A channel id names no destination, so the downgrade drops every account's channel.
    op.execute("UPDATE accounts SET notice_channel_id = NULL")
    op.alter_column("accounts", "notice_channel_id", new_column_name="gate_park_destination_id")
    op.create_foreign_key(
        "accounts_gate_park_destination_id_fkey",
        "accounts",
        "notification_destinations",
        ["gate_park_destination_id"],
        ["id"],
        ondelete="SET NULL",
    )
