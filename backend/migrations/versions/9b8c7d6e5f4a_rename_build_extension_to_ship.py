"""rename build extension to ship

Revision ID: 9b8c7d6e5f4a
Revises: e7d32c9b418f
Create Date: 2026-07-26 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9b8c7d6e5f4a"
down_revision: str | Sequence[str] | None = "e7d32c9b418f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE settings_overrides "
        "SET key = 'extension:ship:' || substring(key from 17) "
        "WHERE key LIKE 'extension:build:%'"
    )
    op.execute(
        "UPDATE durable_runs "
        "SET kind = 'ship.' || substring(kind from 7) "
        "WHERE kind LIKE 'build.%'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE settings_overrides "
        "SET key = 'extension:build:' || substring(key from 16) "
        "WHERE key LIKE 'extension:ship:%'"
    )
    op.execute(
        "UPDATE durable_runs "
        "SET kind = 'build.' || substring(kind from 6) "
        "WHERE kind LIKE 'ship.%'"
    )
