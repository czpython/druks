"""an item holds no run id

Revision ID: b8d1e4a70c93
Revises: a4c9e2f7b1d6
Create Date: 2026-07-29 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8d1e4a70c93"
down_revision: str | Sequence[str] | None = "a4c9e2f7b1d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("work_items", "build_run_id")


def downgrade() -> None:
    op.add_column("work_items", sa.Column("build_run_id", sa.String(), nullable=True))
    op.create_foreign_key(
        "work_items_build_run_id_fkey",
        "work_items",
        "durable_runs",
        ["build_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
