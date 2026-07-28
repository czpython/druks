"""work items store the pr's verdict

Revision ID: a4c9e2f7b1d6
Revises: c71b3d95e802
Create Date: 2026-07-28 09:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4c9e2f7b1d6"
down_revision: str | Sequence[str] | None = "c71b3d95e802"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("work_items", sa.Column("resolution", sa.String(), nullable=True))
    op.add_column(
        "work_items",
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The handoff lane held druks's own verdict; its wording becomes GitHub's, and
    # the row's last touch is the closest stamp the old shape kept.
    op.execute(
        sa.text(
            """
            UPDATE work_items
            SET resolution = CASE status
                    WHEN 'shipped' THEN 'merged'
                    WHEN 'cancelled' THEN 'closed'
                END,
                resolved_at = updated_at
            WHERE status IN ('shipped', 'cancelled')
            """
        )
    )
    op.create_index("work_items_resolved_idx", "work_items", ["resolved_at"])
    op.drop_index("work_items_status_idx", table_name="work_items")
    op.drop_column("work_items", "status")


def downgrade() -> None:
    op.add_column("work_items", sa.Column("status", sa.String(), nullable=True))
    op.execute(
        sa.text(
            """
            UPDATE work_items
            SET status = CASE resolution
                    WHEN 'merged' THEN 'shipped'
                    WHEN 'closed' THEN 'cancelled'
                END
            WHERE resolution IS NOT NULL
            """
        )
    )
    op.create_index("work_items_status_idx", "work_items", ["status"])
    op.drop_index("work_items_resolved_idx", table_name="work_items")
    op.drop_column("work_items", "resolved_at")
    op.drop_column("work_items", "resolution")
