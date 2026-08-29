"""Rename the ship app to software_factory.

Revision ID: c1e8b4a9f2d7
Revises: fe80e8bdf661
Create Date: 2026-08-29
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c1e8b4a9f2d7"
down_revision: str | Sequence[str] | None = "fe80e8bdf661"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE durable_runs SET kind = 'software_factory.build' WHERE kind = 'ship.build'")
    op.execute(
        "UPDATE durable_runs SET kind = 'software_factory.profile' WHERE kind = 'ship.profile'"
    )
    op.execute("UPDATE events SET app = 'software_factory' WHERE app = 'ship'")
    op.execute("UPDATE files SET app = 'software_factory' WHERE app = 'ship'")
    op.execute(
        "UPDATE events SET payload = jsonb_set(payload, '{kind}', '\"software_factory.build\"') "
        "WHERE payload->>'kind' = 'ship.build'"
    )
    op.execute(
        "UPDATE events SET payload = jsonb_set(payload, '{kind}', '\"software_factory.profile\"') "
        "WHERE payload->>'kind' = 'ship.profile'"
    )
    op.execute(
        "UPDATE settings_overrides SET key = 'app:software_factory:' "
        "|| substr(key, length('app:ship:') + 1) "
        "WHERE key LIKE 'app:ship:%'"
    )
    op.execute(
        "UPDATE settings_overrides SET key = 'workflow:software_factory.build:' "
        "|| substr(key, length('workflow:ship.build:') + 1) "
        "WHERE key LIKE 'workflow:ship.build:%'"
    )
    op.execute(
        "UPDATE settings_overrides SET key = 'workflow:software_factory.profile:' "
        "|| substr(key, length('workflow:ship.profile:') + 1) "
        "WHERE key LIKE 'workflow:ship.profile:%'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE settings_overrides SET key = 'app:ship:' "
        "|| substr(key, length('app:software_factory:') + 1) "
        "WHERE starts_with(key, 'app:software_factory:')"
    )
    op.execute(
        "UPDATE settings_overrides SET key = 'workflow:ship.build:' "
        "|| substr(key, length('workflow:software_factory.build:') + 1) "
        "WHERE starts_with(key, 'workflow:software_factory.build:')"
    )
    op.execute(
        "UPDATE settings_overrides SET key = 'workflow:ship.profile:' "
        "|| substr(key, length('workflow:software_factory.profile:') + 1) "
        "WHERE starts_with(key, 'workflow:software_factory.profile:')"
    )
    op.execute(
        "UPDATE events SET payload = jsonb_set(payload, '{kind}', '\"ship.build\"') "
        "WHERE payload->>'kind' = 'software_factory.build'"
    )
    op.execute(
        "UPDATE events SET payload = jsonb_set(payload, '{kind}', '\"ship.profile\"') "
        "WHERE payload->>'kind' = 'software_factory.profile'"
    )
    op.execute("UPDATE files SET app = 'ship' WHERE app = 'software_factory'")
    op.execute("UPDATE events SET app = 'ship' WHERE app = 'software_factory'")
    op.execute("UPDATE durable_runs SET kind = 'ship.build' WHERE kind = 'software_factory.build'")
    op.execute(
        "UPDATE durable_runs SET kind = 'ship.profile' WHERE kind = 'software_factory.profile'"
    )
