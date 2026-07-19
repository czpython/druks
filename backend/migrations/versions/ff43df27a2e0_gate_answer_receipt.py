"""gate answer receipt

Revision ID: ff43df27a2e0
Revises: 0ec9db44973e
Create Date: 2026-07-19 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ff43df27a2e0"
down_revision: str | Sequence[str] | None = "0ec9db44973e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "durable_runs",
        sa.Column("answered_parked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("durable_runs", "answered_parked_at")
