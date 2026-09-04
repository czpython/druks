import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from druks.accounts.models import Account
from druks.harnesses.models import ProviderCatalog, ProviderSubscription
from druks.usage.models import UsageScrape
from druks.user_settings.models import SettingsOverride, UserSettings
from sqlalchemy import text

# Data-only, so it runs inside the suite's rolled-back transaction against the
# current schema.
_MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "migrations"
    / "versions"
    / "4b8d2f6e9a13_one_openai_provider.py"
)


def _upgrade(connection) -> None:
    spec = importlib.util.spec_from_file_location("one_openai_provider", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        module.upgrade()


async def _run_upgrade(druks_db) -> None:
    await druks_db.flush()
    # The rows were seeded through today's model; the migration ran when the
    # table still carried its old name.
    await druks_db.execute(text("ALTER TABLE provider_subscriptions RENAME TO provider_logins"))
    await (await druks_db.connection()).run_sync(_upgrade)


async def _login(provider: str, email: str) -> ProviderSubscription:
    account = await Account.get_or_create(email)
    return await ProviderSubscription.connect(
        provider=provider,
        account=account,
        payload={"tokens": {"access_token": "t"}},
        expires_at=None,
        provider_email=email,
    )


async def _column(druks_db, statement: str) -> list:
    return list((await druks_db.execute(text(statement))).scalars())


async def test_openai_codex_rows_become_openai_everywhere(druks_db):
    codex = await _login("openai-codex", "seat@example.com")
    await _login("openai", "key@example.com")
    await UsageScrape(provider="openai-codex", account_id=codex.account_id, raw_output=None).save()
    await ProviderCatalog.create("openai-codex", [{"id": "openai-codex/gpt-5.5", "label": "sub"}])
    await ProviderCatalog.create("openai", [{"id": "openai/gpt-5.5", "label": "key"}])
    await (await UserSettings.get()).update_profile(default_model="openai-codex/gpt-5.5")
    await SettingsOverride.set_agent_model("implement", "openai-codex/gpt-5-mini")
    await SettingsOverride.set_agent_effort("implement", "openai-codex/keep")

    await _run_upgrade(druks_db)

    assert await _column(druks_db, "SELECT provider FROM provider_logins ORDER BY id") == [
        "openai",
        "openai",
    ]
    assert await _column(druks_db, "SELECT provider FROM usage_scrapes") == ["openai"]
    # The subscription catalog is the one the codex CLI runs; it survives.
    assert await _column(druks_db, "SELECT models -> 0 ->> 'label' FROM provider_catalogs") == [
        "sub"
    ]
    assert await _column(druks_db, "SELECT default_model FROM user_settings") == ["openai/gpt-5.5"]
    assert await _column(
        druks_db, "SELECT value #>> '{}' FROM settings_overrides ORDER BY key"
    ) == ["openai-codex/keep", "openai/gpt-5-mini"]


async def test_an_account_holding_both_openai_logins_fails_by_name(druks_db):
    await _login("openai-codex", "both@example.com")
    await _login("openai", "both@example.com")

    with pytest.raises(RuntimeError, match="both@example.com"):
        await _run_upgrade(druks_db)


async def test_an_unknown_provider_fails(druks_db):
    await _login("mistral", "op@example.com")

    with pytest.raises(RuntimeError, match="mistral"):
        await _run_upgrade(druks_db)
