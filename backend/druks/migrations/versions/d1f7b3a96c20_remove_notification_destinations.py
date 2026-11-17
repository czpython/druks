"""Notification destinations are gone, because nothing sends to one. The notifications
that went to a destination go with them."""

import sqlalchemy as sa
from alembic import op

revision = "d1f7b3a96c20"
down_revision = "c4e8a1f7d2b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM notifications WHERE destination_id IS NOT NULL")
    op.drop_constraint("notifications_one_target_check", "notifications")
    op.drop_column("notifications", "destination_id")
    op.create_check_constraint(
        "notifications_one_target_check",
        "notifications",
        "num_nonnulls(conversation_id, account_id) = 1",
    )
    op.drop_table("notification_destinations")


def downgrade() -> None:
    op.create_table(
        "notification_destinations",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("channel_id", sa.String(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.add_column(
        "notifications",
        sa.Column(
            "destination_id",
            sa.String(),
            sa.ForeignKey("notification_destinations.id"),
            nullable=True,
        ),
    )
    op.drop_constraint("notifications_one_target_check", "notifications")
    op.create_check_constraint(
        "notifications_one_target_check",
        "notifications",
        "num_nonnulls(destination_id, conversation_id, account_id) = 1",
    )
