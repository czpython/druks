"""Scope settings and account ownership.

Revision ID: e6b2a9c41d70
Revises: d2f7a9c4e816
"""

import sqlalchemy as sa
from alembic import op

revision = "e6b2a9c41d70"
down_revision = "d2f7a9c4e816"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "accounts", sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.create_index(
        "accounts_default_idx",
        "accounts",
        ["is_default"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )
    op.rename_table("user_settings", "settings")
    op.add_column("settings", sa.Column("account_id", sa.String(), nullable=True))
    op.create_foreign_key(
        "settings_account_id_fkey",
        "settings",
        "accounts",
        ["account_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "settings_account_id_key", "settings", ["account_id"], postgresql_nulls_not_distinct=True
    )
    op.alter_column("durable_runs", "account_id", server_default=None)
    for table in ("agent_calls", "oauth_connections", "mcp_client_registrations"):
        op.alter_column(table, "account_id", nullable=True, server_default=None)
    for table in ("provider_subscriptions", "provider_keys"):
        op.add_column(
            table, sa.Column("disconnected_at", sa.DateTime(timezone=True), nullable=True)
        )
    op.add_column("agent_calls", sa.Column("subscription_id", sa.String(), nullable=True))
    op.add_column("agent_calls", sa.Column("api_key_provider", sa.String(), nullable=True))
    op.create_foreign_key(
        "agent_calls_subscription_id_fkey",
        "agent_calls",
        "provider_subscriptions",
        ["subscription_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "agent_calls_api_key_provider_fkey",
        "agent_calls",
        "provider_keys",
        ["api_key_provider"],
        ["provider"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        "mcp_client_registrations_server_id_account_id_key",
        "mcp_client_registrations",
        type_="unique",
    )
    op.create_unique_constraint(
        "mcp_client_registrations_server_id_account_id_key",
        "mcp_client_registrations",
        ["server_id", "account_id"],
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    raise NotImplementedError("Account ownership migration is forward-only.")
