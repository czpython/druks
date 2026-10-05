"""A secret ref binds its entry's host.

Revision ID: a3c9e7f1b5d2
Revises: f1a6d3e8c2b7
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3c9e7f1b5d2"
down_revision: str | Sequence[str] | None = "f1a6d3e8c2b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sandbox_secret_refs", sa.Column("host", sa.String(), nullable=False, server_default="")
    )


def downgrade() -> None:
    op.drop_column("sandbox_secret_refs", "host")
