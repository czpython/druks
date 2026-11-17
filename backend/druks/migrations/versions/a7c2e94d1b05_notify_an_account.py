"""A notification can go to one account. Only that account reads it in Druks, and marks it read.
It can be about no run, so it can have no subject."""

import sqlalchemy as sa
from alembic import op

revision = "a7c2e94d1b05"
down_revision = "799bc78c94c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "notifications",
        sa.Column(
            "account_id",
            sa.String(),
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.add_column("notifications", sa.Column("read_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("notifications_account_idx", "notifications", ["account_id", "created_at"])
    op.alter_column("notifications", "subject", nullable=True)
    op.drop_constraint("notifications_one_target_check", "notifications")
    op.create_check_constraint(
        "notifications_one_target_check",
        "notifications",
        "num_nonnulls(destination_id, conversation_id, account_id) = 1",
    )


def downgrade() -> None:
    op.execute("DELETE FROM notifications WHERE account_id IS NOT NULL")
    op.alter_column("notifications", "subject", nullable=False)
    op.drop_constraint("notifications_one_target_check", "notifications")
    op.create_check_constraint(
        "notifications_one_target_check",
        "notifications",
        "num_nonnulls(destination_id, conversation_id) = 1",
    )
    op.drop_index("notifications_account_idx", "notifications")
    op.drop_column("notifications", "read_at")
    op.drop_column("notifications", "account_id")
