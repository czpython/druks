"""Record the source and reused checkpoints of a retry."""

import sqlalchemy as sa
from alembic import op

revision = "f82a73c46190"
down_revision = "c7e2f4a9b1d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("durable_runs", sa.Column("retry_from", sa.String(), nullable=True))
    op.add_column("durable_runs", sa.Column("retry_step", sa.Integer(), nullable=True))
    op.add_column("durable_runs", sa.Column("retry_reused_steps", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("durable_runs", "retry_reused_steps")
    op.drop_column("durable_runs", "retry_step")
    op.drop_column("durable_runs", "retry_from")
