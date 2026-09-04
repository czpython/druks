"""A login is a subscription; an API key is the installation's, one per provider.

Revision ID: 5d3f8a2c7e19
Revises: 4b8d2f6e9a13
Create Date: 2026-09-04
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from druks.secrets import utils

revision: str = "5d3f8a2c7e19"
down_revision: str | Sequence[str] | None = "4b8d2f6e9a13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _reseal_payloads(table: str, *, previous: str) -> None:
    # An encrypted column's AAD is "<table>.<column>", so a payload sealed under
    # the previous table name opens only under it.
    bind = op.get_bind()
    for row_id, envelope in bind.execute(sa.text(f"SELECT id, payload FROM {table}")).all():
        plaintext = utils.decrypt(bytes(envelope), f"{previous}.payload")
        bind.execute(
            sa.text(f"UPDATE {table} SET payload = :payload WHERE id = :id").bindparams(
                payload=utils.encrypt(plaintext, f"{table}.payload"), id=row_id
            )
        )


def upgrade() -> None:
    op.create_table(
        "provider_keys",
        sa.Column("provider", sa.String(), primary_key=True),
        sa.Column("value", sa.LargeBinary(), nullable=False),
        sa.Column(
            "updated_by_account_id",
            sa.String(),
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    bind = op.get_bind()
    # One key per provider: two people's keys cannot both become the
    # installation's. Name them so the operator removes one.
    twice = bind.execute(
        sa.text(
            "SELECT provider_logins.provider, "
            "string_agg(accounts.username, ', ' ORDER BY accounts.username) "
            "FROM provider_logins JOIN accounts ON accounts.id = provider_logins.account_id "
            "WHERE provider_logins.kind = 'api_key' "
            "GROUP BY provider_logins.provider HAVING count(*) > 1"
        )
    ).all()
    if twice:
        held = "; ".join(f"{provider}: {users}" for provider, users in twice)
        raise RuntimeError(f"more than one API key per provider ({held}); remove all but one first")
    # A person's key row becomes the provider's key; its owner was the paster.
    rows = bind.execute(
        sa.text(
            "SELECT provider, account_id, payload, updated_at FROM provider_logins "
            "WHERE kind = 'api_key'"
        )
    ).all()
    for provider, account_id, envelope, updated_at in rows:
        payload = json.loads(utils.decrypt(bytes(envelope), "provider_logins.payload"))
        bind.execute(
            sa.text(
                "INSERT INTO provider_keys (provider, value, updated_by_account_id, updated_at) "
                "VALUES (:provider, :value, :account_id, :updated_at)"
            ).bindparams(
                provider=provider,
                value=utils.encrypt(payload["api_key"].encode(), "provider_keys.value"),
                account_id=account_id,
                updated_at=updated_at,
            )
        )
    op.execute(sa.text("DELETE FROM provider_logins WHERE kind = 'api_key'"))
    op.drop_column("provider_logins", "kind")
    op.drop_constraint("provider_logins_provider_account_id_key", "provider_logins")
    op.rename_table("provider_logins", "provider_subscriptions")
    _reseal_payloads("provider_subscriptions", previous="provider_logins")
    op.create_unique_constraint(
        "provider_subscriptions_provider_account_id_key",
        "provider_subscriptions",
        ["provider", "account_id"],
    )


def downgrade() -> None:
    keyed = op.get_bind().scalar(sa.text("SELECT count(*) FROM provider_keys"))
    if keyed:
        raise RuntimeError(f"{keyed} provider keys cannot be handed back to one account")
    op.drop_table("provider_keys")
    op.drop_constraint("provider_subscriptions_provider_account_id_key", "provider_subscriptions")
    op.rename_table("provider_subscriptions", "provider_logins")
    _reseal_payloads("provider_logins", previous="provider_subscriptions")
    op.create_unique_constraint(
        "provider_logins_provider_account_id_key", "provider_logins", ["provider", "account_id"]
    )
    op.add_column(
        "provider_logins",
        sa.Column("kind", sa.String(), nullable=False, server_default="oauth"),
    )
