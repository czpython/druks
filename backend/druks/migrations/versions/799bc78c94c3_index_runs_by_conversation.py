"""Index the runs that a conversation started, so the Chat page reads its active runs
without a scan of every run."""

import sqlalchemy as sa
from alembic import op

revision = "799bc78c94c3"
down_revision = "c3f9a1d27b64"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "durable_runs_conversation_idx",
        "durable_runs",
        ["conversation_id"],
        postgresql_where=sa.text("conversation_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("durable_runs_conversation_idx", table_name="durable_runs")
