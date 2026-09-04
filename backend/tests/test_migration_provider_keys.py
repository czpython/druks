import importlib.util
import json
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from druks.accounts.models import Account
from druks.harnesses.models import ProviderKey, ProviderSubscription
from druks.secrets import utils
from sqlalchemy import text

# Postgres DDL is transactional, so the test rebuilds the pre-migration shape
# inside the suite's rolled-back transaction and runs the real upgrade on it.
_MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "migrations"
    / "versions"
    / "5d3f8a2c7e19_provider_keys.py"
)


def _upgrade(connection) -> None:
    spec = importlib.util.spec_from_file_location("provider_keys", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        module.upgrade()


async def _pre_migration_shape(druks_db) -> None:
    await druks_db.execute(text("DROP TABLE provider_keys"))
    await druks_db.execute(text("ALTER TABLE provider_subscriptions RENAME TO provider_logins"))
    await druks_db.execute(
        text(
            "ALTER TABLE provider_logins RENAME CONSTRAINT "
            "provider_subscriptions_provider_account_id_key "
            "TO provider_logins_provider_account_id_key"
        )
    )
    await druks_db.execute(
        text("ALTER TABLE provider_logins ADD COLUMN kind varchar NOT NULL DEFAULT 'oauth'")
    )


async def _login(druks_db, provider: str, email: str, *, kind: str, payload: dict) -> None:
    account = await Account.get_or_create(email)
    await druks_db.execute(
        text(
            "INSERT INTO provider_logins "
            "(id, provider, account_id, provider_email, kind, payload, expires_at, updated_at) "
            "VALUES (:id, :provider, :account_id, :email, :kind, :payload, NULL, now())"
        ),
        {
            "id": f"{provider}-{email}",
            "provider": provider,
            "account_id": account.id,
            "email": email,
            "kind": kind,
            "payload": utils.encrypt(json.dumps(payload).encode(), "provider_logins.payload"),
        },
    )


async def test_a_persons_key_row_becomes_the_providers_key(druks_db):
    await _pre_migration_shape(druks_db)
    await _login(druks_db, "anthropic", "seat@example.com", kind="oauth", payload={"t": 1})
    await _login(
        druks_db, "openai", "keyholder@example.com", kind="api_key", payload={"api_key": "sk-4f2a"}
    )

    await (await druks_db.connection()).run_sync(_upgrade)

    [stored] = await ProviderKey.list_all()
    assert stored.provider == "openai"
    assert stored.value.decrypt() == "sk-4f2a"
    assert stored.updated_by.username == "keyholder@example.com"
    # The login row is a subscription now, resealed under the new table name.
    [subscription] = await ProviderSubscription.list_all()
    assert subscription.provider == "anthropic"
    assert dict(subscription.payload) == {"t": 1}
    columns = await druks_db.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'provider_subscriptions'"
        )
    )
    assert "kind" not in list(columns.scalars())


async def test_two_keys_for_one_provider_fail_by_name(druks_db):
    await _pre_migration_shape(druks_db)
    await _login(druks_db, "openai", "a@example.com", kind="api_key", payload={"api_key": "1"})
    await _login(druks_db, "openai", "b@example.com", kind="api_key", payload={"api_key": "2"})

    with pytest.raises(RuntimeError, match="openai: a@example.com, b@example.com"):
        await (await druks_db.connection()).run_sync(_upgrade)
