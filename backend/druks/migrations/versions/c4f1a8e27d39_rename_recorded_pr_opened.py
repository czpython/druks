"""Rename recorded pr.opened rows: Software Factory records build.pr_opened.

Revision ID: c4f1a8e27d39
Revises: d8f6a2c91b47
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4f1a8e27d39"
down_revision: str | Sequence[str] | None = "d8f6a2c91b47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE events SET type = 'build.pr_opened' "
            "WHERE type = 'pr.opened' AND app = 'software_factory'"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE events SET type = 'pr.opened' "
            "WHERE type = 'build.pr_opened' AND app = 'software_factory'"
        )
    )
