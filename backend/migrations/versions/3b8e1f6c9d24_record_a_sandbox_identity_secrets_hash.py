"""Record the secrets hash of the config that created a sandbox.

Revision ID: 3b8e1f6c9d24
Revises: 9e47b68530e2
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3b8e1f6c9d24"
down_revision: str | Sequence[str] | None = "9e47b68530e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sandbox_identities",
        sa.Column("secrets_hash", sa.String(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("sandbox_identities", "secrets_hash")
