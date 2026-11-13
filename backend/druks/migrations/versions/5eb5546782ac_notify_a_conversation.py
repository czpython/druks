"""A notification posts to a destination or in the conversation that started its run."""

import sqlalchemy as sa
from alembic import op

revision = "5eb5546782ac"
down_revision = "5cf17f5e02c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "notifications",
        sa.Column(
            "conversation_id",
            sa.String(),
            sa.ForeignKey("chat_conversations.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.alter_column("notifications", "destination_id", nullable=True)
    op.create_check_constraint(
        "notifications_one_target_check",
        "notifications",
        "num_nonnulls(destination_id, conversation_id) = 1",
    )


def downgrade() -> None:
    op.execute("DELETE FROM notifications WHERE conversation_id IS NOT NULL")
    op.drop_constraint("notifications_one_target_check", "notifications")
    op.alter_column("notifications", "destination_id", nullable=False)
    op.drop_column("notifications", "conversation_id")
