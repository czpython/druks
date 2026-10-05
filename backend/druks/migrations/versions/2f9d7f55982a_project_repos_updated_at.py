"""Stamp updated_at on project repos.

Revision ID: 2f9d7f55982a
Revises: 6d69d94c96e1
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2f9d7f55982a"
down_revision: str | Sequence[str] | None = "6d69d94c96e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "project_repos", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute("UPDATE project_repos SET updated_at = created_at")
    op.alter_column("project_repos", "updated_at", nullable=False)


def downgrade() -> None:
    op.drop_column("project_repos", "updated_at")
