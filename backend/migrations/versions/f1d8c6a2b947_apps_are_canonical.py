"""Use app identity surfaces.

Revision ID: f1d8c6a2b947
Revises: b6d4f2a81c93
Create Date: 2026-08-23
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f1d8c6a2b947"
down_revision: str | Sequence[str] | None = "b6d4f2a81c93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE events RENAME COLUMN extension TO app")
    op.execute(
        "UPDATE settings_overrides "
        "SET key = 'app:' || substr(key, 11) "
        "WHERE key LIKE 'extension:%'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE settings_overrides "
        "SET key = 'extension:' || substr(key, 5) "
        "WHERE key LIKE 'app:%'"
    )
    op.execute("ALTER TABLE events RENAME COLUMN app TO extension")
