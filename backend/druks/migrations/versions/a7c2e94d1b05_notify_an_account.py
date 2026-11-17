"""A notification can go to one account, as a notice for that person only, which they
mark read in their inbox. A person's notice can be about no run, so it has no subject."""

import sqlalchemy as sa
from alembic import op

revision = "a7c2e94d1b05"
down_revision = "c3f9a1d27b64"
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
    op.drop_column("notifications", "read_at")
    op.drop_column("notifications", "account_id")
