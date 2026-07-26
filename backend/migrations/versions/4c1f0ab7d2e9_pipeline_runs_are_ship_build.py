"""pipeline runs are ship.build

Revision ID: 4c1f0ab7d2e9
Revises: 9b8c7d6e5f4a
Create Date: 2026-07-26 13:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4c1f0ab7d2e9"
down_revision: str | Sequence[str] | None = "9b8c7d6e5f4a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The workflow class is Build, so its kind is ship.build. The prefix swap in
    # 9b8c7d6e5f4a carried the old class name through to ship.build_workflow, which
    # matches no registered workflow; both spellings land on the real kind here.
    op.execute(
        "UPDATE durable_runs SET kind = 'ship.build' "
        "WHERE kind IN ('ship.build_workflow', 'build.build_workflow')"
    )


def downgrade() -> None:
    op.execute("UPDATE durable_runs SET kind = 'ship.build_workflow' WHERE kind = 'ship.build'")
