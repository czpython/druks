"""A service holds at most one grant of its own.

Revision ID: b7d2e4a9c1f3
Revises: 9e4b7c2d1a68
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7d2e4a9c1f3"
down_revision: str | Sequence[str] | None = "9e4b7c2d1a68"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_vault_one_service_grant",
        "vault",
        ["audience"],
        unique=True,
        postgresql_where=sa.text("kind = 'oauth' AND account_id IS NULL AND revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_vault_one_service_grant", table_name="vault")
