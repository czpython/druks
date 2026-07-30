"""migrate plan gate overrides

Revision ID: c2f6a9d8137e
Revises: b8d1e4a70c93
Create Date: 2026-07-30 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c2f6a9d8137e"
down_revision: str | Sequence[str] | None = "b8d1e4a70c93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE settings_overrides "
        "SET key = 'workflow:ship.build:plan_gate', "
        "value = CASE value "
        "WHEN 'true'::jsonb THEN '\"machine\"'::jsonb "
        "ELSE '\"human\"'::jsonb END "
        "WHERE key = 'workflow:ship.build:auto_dispatch_on_plan_approval'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE settings_overrides "
        "SET key = 'workflow:ship.build:auto_dispatch_on_plan_approval', "
        "value = CASE value "
        "WHEN '\"machine\"'::jsonb THEN 'true'::jsonb "
        "ELSE 'false'::jsonb END "
        "WHERE key = 'workflow:ship.build:plan_gate'"
    )
