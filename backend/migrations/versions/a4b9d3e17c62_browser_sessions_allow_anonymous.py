"""Browser sessions allow the anonymous status.

Revision ID: a4b9d3e17c62
Revises: f1d8c6a2b947
Create Date: 2026-08-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a4b9d3e17c62"
down_revision: str | Sequence[str] | None = "f1d8c6a2b947"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("browser_sessions_status_check", "browser_sessions", type_="check")
    op.create_check_constraint(
        "browser_sessions_status_check",
        "browser_sessions",
        "status IN ('needs_login', 'ready', 'stale', 'anonymous')",
    )


def downgrade() -> None:
    op.drop_constraint("browser_sessions_status_check", "browser_sessions", type_="check")
    op.create_check_constraint(
        "browser_sessions_status_check",
        "browser_sessions",
        "status IN ('needs_login', 'ready', 'stale')",
    )
