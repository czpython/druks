"""A run records when its ask expires, so Druks can cancel a run that no worker times out."""

import sqlalchemy as sa
from alembic import op

revision = "11c6087812ab"
down_revision = "5eb5546782ac"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "durable_runs", sa.Column("input_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    # A run parked before this revision gets the default gate timeout.
    op.execute(
        "UPDATE durable_runs SET input_expires_at = input_requested_at + interval '14 days' "
        "WHERE input_gate IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("durable_runs", "input_expires_at")
