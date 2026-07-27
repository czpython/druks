"""every work item has a ticket key

Revision ID: d3a5c71f8e40
Revises: c8f04a1e9b27
Create Date: 2026-07-27 01:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3a5c71f8e40"
down_revision: str | Sequence[str] | None = "c8f04a1e9b27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("work_items", "remote_key", existing_type=sa.String(), nullable=False)
    # The uniqueness no longer needs to skip nulls.
    op.drop_index("work_items_remote_unique", table_name="work_items")
    op.create_index("work_items_remote_unique", "work_items", ["source", "remote_key"], unique=True)


def downgrade() -> None:
    op.drop_index("work_items_remote_unique", table_name="work_items")
    op.create_index(
        "work_items_remote_unique",
        "work_items",
        ["source", "remote_key"],
        unique=True,
        postgresql_where=sa.text("remote_key IS NOT NULL"),
    )
    op.alter_column("work_items", "remote_key", existing_type=sa.String(), nullable=True)
