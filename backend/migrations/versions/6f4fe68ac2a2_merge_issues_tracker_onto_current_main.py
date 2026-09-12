"""Join the issues-tracker chain onto current main.

Revision ID: 6f4fe68ac2a2
Revises: b43924bf37db, b8f3c6d1a047
Create Date: 2026-09-11
"""

from collections.abc import Sequence

revision: str = "6f4fe68ac2a2"
down_revision: str | Sequence[str] | None = ("b43924bf37db", "b8f3c6d1a047")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
