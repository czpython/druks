"""Transfer installation ownership.

Revision ID: f7c3b0d52e81
Revises: e6b2a9c41d70
"""

import sqlalchemy as sa
from alembic import op

revision = "f7c3b0d52e81"
down_revision = "e6b2a9c41d70"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    # The installation row used an explicit ID. The next insert needs a free ID.
    connection.execute(
        sa.text("""
        SELECT setval(pg_get_serial_sequence('settings', 'id'),
                      COALESCE(MAX(id), 1), COUNT(*) > 0) FROM settings
    """)
    )
    connection.execute(
        sa.text("""
        UPDATE accounts SET is_default = true
        WHERE id = COALESCE(
            (SELECT NULLIF(fallback_account_id, 'system') FROM settings WHERE account_id IS NULL),
            (SELECT id FROM accounts WHERE id != 'system' ORDER BY created_at, id LIMIT 1)
        )
    """)
    )
    connection.execute(
        sa.text("""
        UPDATE durable_runs SET account_id = (SELECT id FROM accounts WHERE is_default)
        WHERE account_id = 'system'
    """)
    )
    connection.execute(
        sa.text("""
        UPDATE agent_calls AS call SET subscription_id = subscription.id
        FROM provider_subscriptions AS subscription
        WHERE call.account_id = subscription.account_id
          AND split_part(call.model, '/', 1) = subscription.provider
    """)
    )
    connection.execute(
        sa.text("""
        UPDATE agent_calls AS call SET api_key_provider = key.provider
        FROM provider_keys AS key
        WHERE call.account_id = 'system' AND split_part(call.model, '/', 1) = key.provider
    """)
    )
    # Unmatched historical billing must be reconciled before enforcing references.
    op.create_check_constraint(
        "agent_calls_billing_source_check",
        "agent_calls",
        "(subscription_id IS NOT NULL) <> (api_key_provider IS NOT NULL)",
    )
    op.drop_index("agent_calls_account_finished_idx", table_name="agent_calls")
    op.drop_column("agent_calls", "account_id")
    op.create_index(
        "agent_calls_subscription_finished_idx", "agent_calls", ["subscription_id", "finished_at"]
    )
    for table in ("oauth_connections", "mcp_client_registrations"):
        connection.execute(
            sa.text(f"UPDATE {table} SET account_id = NULL WHERE account_id = 'system'")
        )
    connection.execute(sa.text("UPDATE files SET uploaded_by = NULL WHERE uploaded_by = 'system'"))
    connection.execute(sa.text("DELETE FROM accounts WHERE id = 'system'"))


def downgrade() -> None:
    raise NotImplementedError("Account ownership migration is forward-only.")
