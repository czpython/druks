"""A secret ref names every resource its one token covers.

Revision ID: 8c2f4e6a1d37
Revises: 5574b6ec5d33
Create Date: 2026-10-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "8c2f4e6a1d37"
down_revision: str | Sequence[str] | None = "5574b6ec5d33"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sandbox_secret_refs",
        sa.Column("resources", postgresql.JSONB(), nullable=False, server_default="[]"),
    )
    op.execute(
        "UPDATE sandbox_secret_refs SET resources = jsonb_build_array(resource) "
        "WHERE resource <> ''"
    )
    op.drop_column("sandbox_secret_refs", "resource")


def downgrade() -> None:
    op.add_column(
        "sandbox_secret_refs",
        sa.Column("resource", sa.String(), nullable=False, server_default=""),
    )
    op.execute(
        "UPDATE sandbox_secret_refs SET resource = resources ->> 0 WHERE resources <> '[]'"
    )
    op.drop_column("sandbox_secret_refs", "resources")
