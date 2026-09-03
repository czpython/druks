"""Providers own logins and quota.

Revision ID: e3a9c7d1b5f4
Revises: b4c7e1a8d052
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from druks.secrets import utils

revision: str = "e3a9c7d1b5f4"
down_revision: str | Sequence[str] | None = "b4c7e1a8d052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The two subscription harnesses each ran one provider; a key harness's
# provider is the namespace of the model it was set to run.
_OAUTH_PROVIDER_BY_HARNESS = {"claude": "anthropic", "codex": "openai-codex"}
_PROVIDERS = ("anthropic", "openai", "openai-codex")


def _reseal_payloads(table: str, *, previous: str) -> None:
    # An encrypted column's AAD is "<table>.<column>", so a payload sealed under
    # the previous table name opens only under it. Reseal every row under the
    # name the model now reads with.
    bind = op.get_bind()
    for login_id, envelope in bind.execute(sa.text(f"SELECT id, payload FROM {table}")).all():
        plaintext = utils.decrypt(bytes(envelope), f"{previous}.payload")
        bind.execute(
            sa.text(f"UPDATE {table} SET payload = :payload WHERE id = :id").bindparams(
                payload=utils.encrypt(plaintext, f"{table}.payload"), id=login_id
            )
        )


def upgrade() -> None:
    op.rename_table("harness_logins", "provider_logins")
    _reseal_payloads("provider_logins", previous="harness_logins")
    op.alter_column("provider_logins", "harness", new_column_name="provider")
    # Two harness rows can map onto one provider for one account; the guard
    # below names that case, so the unique constraint returns only after it.
    op.drop_constraint("harness_logins_harness_account_id_key", "provider_logins")
    op.execute(sa.text("UPDATE provider_logins SET kind = 'oauth' WHERE kind = 'subscription'"))
    for harness, provider in _OAUTH_PROVIDER_BY_HARNESS.items():
        op.execute(
            sa.text(
                "UPDATE provider_logins SET provider = :provider WHERE provider = :harness"
            ).bindparams(provider=provider, harness=harness)
        )
    op.execute(
        sa.text(
            "UPDATE provider_logins SET provider = split_part(harnesses.model, '/', 1) "
            "FROM harnesses WHERE harnesses.name = provider_logins.provider "
            "AND provider_logins.kind = 'api_key' AND position('/' IN harnesses.model) > 0"
        )
    )
    bind = op.get_bind()
    unmapped = bind.scalar(
        sa.text("SELECT count(*) FROM provider_logins WHERE provider NOT IN :providers").bindparams(
            sa.bindparam("providers", expanding=True, value=list(_PROVIDERS))
        )
    )
    if unmapped:
        raise RuntimeError(f"{unmapped} login rows name no provider; reconnect them first")
    collisions = bind.scalar(
        sa.text(
            "SELECT count(*) FROM (SELECT 1 FROM provider_logins "
            "GROUP BY provider, account_id HAVING count(*) > 1) AS twice"
        )
    )
    if collisions:
        raise RuntimeError(
            f"{collisions} accounts hold two logins for one provider; disconnect one first"
        )
    op.create_unique_constraint(
        "provider_logins_provider_account_id_key", "provider_logins", ["provider", "account_id"]
    )

    op.drop_index("usage_scrapes_account_harness_time_idx", table_name="usage_scrapes")
    op.alter_column("usage_scrapes", "harness", new_column_name="provider")
    for harness, provider in _OAUTH_PROVIDER_BY_HARNESS.items():
        op.execute(
            sa.text(
                "UPDATE usage_scrapes SET provider = :provider WHERE provider = :harness"
            ).bindparams(provider=provider, harness=harness)
        )
    op.create_index(
        "usage_scrapes_account_provider_time_idx",
        "usage_scrapes",
        ["account_id", "provider", "scraped_at"],
    )


def downgrade() -> None:
    keyed = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM provider_logins WHERE kind = 'api_key'")
    )
    if keyed:
        raise RuntimeError(f"{keyed} api_key logins cannot be handed back to one harness")
    op.drop_index("usage_scrapes_account_provider_time_idx", table_name="usage_scrapes")
    for harness, provider in _OAUTH_PROVIDER_BY_HARNESS.items():
        op.execute(
            sa.text(
                "UPDATE usage_scrapes SET provider = :harness WHERE provider = :provider"
            ).bindparams(provider=provider, harness=harness)
        )
    op.alter_column("usage_scrapes", "provider", new_column_name="harness")
    op.create_index(
        "usage_scrapes_account_harness_time_idx",
        "usage_scrapes",
        ["account_id", "harness", "scraped_at"],
    )

    for harness, provider in _OAUTH_PROVIDER_BY_HARNESS.items():
        op.execute(
            sa.text(
                "UPDATE provider_logins SET provider = :harness WHERE provider = :provider"
            ).bindparams(provider=provider, harness=harness)
        )
    op.execute(sa.text("UPDATE provider_logins SET kind = 'subscription' WHERE kind = 'oauth'"))
    op.drop_constraint("provider_logins_provider_account_id_key", "provider_logins")
    op.alter_column("provider_logins", "provider", new_column_name="harness")
    op.rename_table("provider_logins", "harness_logins")
    _reseal_payloads("harness_logins", previous="provider_logins")
    op.create_unique_constraint(
        "harness_logins_harness_account_id_key", "harness_logins", ["harness", "account_id"]
    )
