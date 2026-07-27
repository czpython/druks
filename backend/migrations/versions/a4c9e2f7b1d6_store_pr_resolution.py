"""store pull request resolution

Revision ID: a4c9e2f7b1d6
Revises: f2c8b81a9d4e
Create Date: 2026-07-27 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4c9e2f7b1d6"
down_revision: str | Sequence[str] | None = "f2c8b81a9d4e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("work_items", sa.Column("pr_merged", sa.Boolean(), nullable=True))
    op.add_column(
        "work_items",
        sa.Column("pr_resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE work_items
            SET pr_merged = CASE status
                    WHEN 'shipped' THEN true
                    WHEN 'cancelled' THEN false
                END,
                pr_resolved_at = updated_at
            WHERE status IN ('shipped', 'cancelled')
            """
        )
    )
    op.drop_index("work_items_status_idx", table_name="work_items")
    op.drop_column("work_items", "status")


def downgrade() -> None:
    op.add_column("work_items", sa.Column("status", sa.String(), nullable=True))
    op.execute(
        sa.text(
            """
            UPDATE work_items
            SET status = CASE
                    WHEN pr_merged = true THEN 'shipped'
                    WHEN pr_merged = false THEN 'cancelled'
                END
            WHERE pr_merged IS NOT NULL
            """
        )
    )
    op.create_index("work_items_status_idx", "work_items", ["status"], unique=False)
    op.drop_column("work_items", "pr_resolved_at")
    op.drop_column("work_items", "pr_merged")
