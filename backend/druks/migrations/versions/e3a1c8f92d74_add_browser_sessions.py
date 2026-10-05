"""add browser sessions

Revision ID: e3a1c8f92d74
Revises: c9e5b1d47a26
Create Date: 2026-08-12 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e3a1c8f92d74"
down_revision: str | Sequence[str] | None = "c9e5b1d47a26"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "browser_sessions",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("payload_format", sa.String(length=16), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column("site", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(), nullable=False),
        sa.CheckConstraint(
            "payload_format IN ('storage_state', 'profile_dir')",
            name="browser_sessions_payload_format_check",
        ),
        sa.CheckConstraint(
            "status IN ('needs_login', 'ready', 'stale')",
            name="browser_sessions_status_check",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )


def downgrade() -> None:
    op.drop_table("browser_sessions")
