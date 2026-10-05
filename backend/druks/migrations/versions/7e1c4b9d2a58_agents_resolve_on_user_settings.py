"""An agent resolves to harness, model, and billing from the operator's defaults.

Revision ID: 7e1c4b9d2a58
Revises: 5d3f8a2c7e19
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7e1c4b9d2a58"
down_revision: str | Sequence[str] | None = "5d3f8a2c7e19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_HARNESS = "claude"
_DEFAULT_BILLING = "subscription"


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("default_harness", sa.String(), nullable=False, server_default=_DEFAULT_HARNESS),
    )
    op.add_column(
        "user_settings",
        sa.Column("default_billing", sa.String(), nullable=False, server_default=_DEFAULT_BILLING),
    )
    op.add_column(
        "user_settings",
        sa.Column("default_effort", sa.String(), nullable=False, server_default="high"),
    )
    op.add_column(
        "user_settings",
        sa.Column("fast_mode", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "user_settings",
        sa.Column("default_timeout", sa.Integer(), nullable=False, server_default="1800"),
    )
    # The first harness row carried the effort, timeout, and fast mode most
    # agents ran with; they become the one set of defaults.
    op.execute(
        sa.text(
            "UPDATE user_settings SET default_effort = first.effort, "
            "default_timeout = first.timeout, fast_mode = first.fast_mode "
            "FROM (SELECT effort, timeout, fast_mode FROM harnesses ORDER BY name LIMIT 1) AS first"
        )
    )
    op.drop_table("harnesses")


def downgrade() -> None:
    op.create_table(
        "harnesses",
        sa.Column("name", sa.String(), primary_key=True),
        sa.Column("fast_mode", sa.Boolean(), nullable=False),
        sa.Column("effort", sa.String(), nullable=False),
        sa.Column("timeout", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute(
        sa.text(
            "INSERT INTO harnesses (name, fast_mode, effort, timeout, updated_at) "
            "SELECT harness, fast_mode, default_effort, default_timeout, now() "
            "FROM user_settings, unnest(ARRAY['claude', 'codex', 'opencode', 'pi']) AS harness"
        )
    )
    for column in ("default_timeout", "fast_mode", "default_effort", "default_billing", "default_harness"):
        op.drop_column("user_settings", column)
