"""rename work item ticket fields

Revision ID: f2c8b81a9d4e
Revises: d3a5c71f8e40
Create Date: 2026-07-27 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2c8b81a9d4e"
down_revision: str | Sequence[str] | None = "d3a5c71f8e40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("work_items_remote_unique", table_name="work_items")
    op.alter_column(
        "work_items",
        "remote_key",
        existing_type=sa.String(),
        existing_nullable=False,
        new_column_name="ticket_key",
    )
    op.alter_column(
        "work_items",
        "remote_url",
        existing_type=sa.String(),
        existing_nullable=True,
        new_column_name="ticket_url",
    )
    op.create_index(
        "work_items_ticket_unique",
        "work_items",
        ["source", "ticket_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("work_items_ticket_unique", table_name="work_items")
    op.alter_column(
        "work_items",
        "ticket_key",
        existing_type=sa.String(),
        existing_nullable=False,
        new_column_name="remote_key",
    )
    op.alter_column(
        "work_items",
        "ticket_url",
        existing_type=sa.String(),
        existing_nullable=True,
        new_column_name="remote_url",
    )
    op.create_index(
        "work_items_remote_unique",
        "work_items",
        ["source", "remote_key"],
        unique=True,
    )
