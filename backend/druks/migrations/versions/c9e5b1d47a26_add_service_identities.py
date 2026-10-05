"""add service identities

Revision ID: c9e5b1d47a26
Revises: a4c8e2f7b913
Create Date: 2026-08-09 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c9e5b1d47a26"
down_revision: str | Sequence[str] | None = "a4c8e2f7b913"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "service_identities",
        sa.Column("service", sa.String(), nullable=False),
        sa.Column("identity", postgresql.JSONB(), nullable=False),
        sa.Column("secrets", sa.LargeBinary(), nullable=False),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("service"),
    )


def downgrade() -> None:
    op.drop_table("service_identities")
