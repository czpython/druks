import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from conftest import (
    CONFIG_PROBE,
    IDENTITY_HEADER,
    connect_anthropic_subscription,
    header_client,
    settings_client,
)
from druks.accounts.exceptions import AuthConfigurationError
from druks.accounts.models import Account
from druks.database import db_session
from druks.durable.models import Run
from druks.harnesses.config import get_config
from druks.harnesses.exceptions import HarnessNotConnectedError
from druks.notifications.models import Destination
from druks.secrets.datastructures import Audience
from druks.secrets.models import VaultSecret
from druks.testing import make_settings
from druks.user_settings.models import InstallationSettings, SettingsOverride
from druks.workflows import _run_instance
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError


async def test_account_creation_copies_defaults_once(tmp_path, druks_db, monkeypatch):
    destination = await Destination.create(
        name="New account gates", kind="slack_webhook", url="https://example.invalid/hook"
    )
    installation = await InstallationSettings.get()
    await installation.update(gate_park_destination_id=destination.id)
    monkeypatch.setattr(
        "druks.accounts.models.load_settings",
        lambda: make_settings(tmp_path, timezone="Europe/Madrid"),
    )
    alice = await Account.get_or_create("alice@example.com")
    assert (alice.timezone, alice.gate_park_destination_id) == ("Europe/Madrid", destination.id)
    await alice.update_preferences(timezone="America/New_York", gate_park_destination_id=None)
    await installation.update(default_effort="low")
    existing = await Account.get_or_create("alice@example.com")
    assert existing.id == alice.id
    assert (existing.timezone, existing.gate_park_destination_id) == ("America/New_York", None)
    with header_client(tmp_path) as client:
        assert client.get(
            "/api/settings/personal", headers={IDENTITY_HEADER: alice.username}
        ).json() == {
            "timezone": "America/New_York",
            "gateParkDestinationId": None,
        }
    assert await druks_db.scalar(select(func.count()).select_from(InstallationSettings)) == 1


async def test_personal_notifications_can_be_saved_and_cleared(tmp_path, druks_db):
    destination = await Destination.create(
        name="Personal gates", kind="slack_webhook", url="https://example.invalid/hook"
    )
    with settings_client(tmp_path) as client:
        saved = client.patch(
            "/api/settings/personal", json={"gateParkDestinationId": destination.id}
        )
        assert saved.status_code == 200
        assert saved.json()["gateParkDestinationId"] == destination.id
        assert client.get("/api/settings").json()["gateParkDestinationId"] is None
        cleared = client.patch("/api/settings/personal", json={"gateParkDestinationId": None})
        assert cleared.status_code == 200
        assert cleared.json()["gateParkDestinationId"] is None
        rejected = client.patch("/api/settings/personal", json={"gateParkDestinationId": "missing"})
        assert rejected.status_code == 422


@pytest.mark.parametrize("has_settings", [False, True])
async def test_migrations_preserve_preferences_and_installation_execution(druks_db, has_settings):
    alice = await Account.get_or_create("alice@example.com")
    bob = await Account.get_or_create("bob@example.com")
    charlie = await Account.get_or_create("charlie@example.com")
    account_ids = (alice.id, bob.id, charlie.id)
    destination = await Destination.create(
        name="Personal gates", kind="slack_webhook", url="https://example.invalid/hook"
    )
    for statement in (
        "DELETE FROM settings",
        "ALTER TABLE accounts DROP COLUMN timezone",
        "ALTER TABLE accounts DROP COLUMN gate_park_destination_id",
        "ALTER TABLE settings DROP CONSTRAINT settings_singleton",
        "ALTER TABLE settings ADD COLUMN timezone varchar NOT NULL DEFAULT 'UTC'",
        "ALTER TABLE settings ADD COLUMN account_id varchar REFERENCES accounts(id) "
        "ON DELETE CASCADE",
        "ALTER TABLE settings ADD CONSTRAINT settings_account_id_key "
        "UNIQUE NULLS NOT DISTINCT (account_id)",
    ):
        await druks_db.execute(text(statement))
    if has_settings:
        for row_id, account_id, timezone, destination_id in (
            (7, None, "Europe/Madrid", destination.id),
            (1, alice.id, "America/New_York", destination.id),
            (2, bob.id, "Asia/Tokyo", None),
        ):
            await druks_db.execute(
                text(
                    "INSERT INTO settings (id, account_id, timezone, gate_park_destination_id, "
                    "default_harness, default_model, default_billing, default_effort, fast_mode, "
                    "default_timeout, updated_at) VALUES (:id, :account_id, :timezone, "
                    ":destination_id, 'codex', 'openai/gpt-5.5', 'api_key', "
                    "'high', true, 600, '2026-09-01T12:00:00Z')"
                ),
                dict(
                    id=row_id,
                    account_id=account_id,
                    timezone=timezone,
                    destination_id=destination_id,
                ),
            )
        await druks_db.execute(
            text("UPDATE settings SET default_effort = 'low' WHERE account_id IS NOT NULL")
        )

    def upgrade(connection):
        for filename in (
            "74b53981122c_add_account_preferences.py",
            "7dc609a2d51a_move_preferences_to_accounts.py",
            "b43924bf37db_enforce_installation_settings_singleton.py",
        ):
            path = Path(__file__).resolve().parent.parent / "migrations" / "versions" / filename
            spec = importlib.util.spec_from_file_location("settings_migration", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with Operations.context(MigrationContext.configure(connection)):
                module.upgrade()

    await (await druks_db.connection()).run_sync(upgrade)
    preferences = (
        await druks_db.execute(
            text("SELECT id, timezone, gate_park_destination_id FROM accounts ORDER BY id")
        )
    ).all()
    expected = (
        [
            (alice.id, "America/New_York", destination.id),
            (bob.id, "Asia/Tokyo", None),
            (charlie.id, "Europe/Madrid", destination.id),
        ]
        if has_settings
        else [(account_id, "UTC", None) for account_id in account_ids]
    )
    assert preferences == sorted(expected)
    execution = (
        await druks_db.execute(
            text(
                "SELECT id, default_harness, default_model, default_billing, default_effort, "
                "fast_mode, default_timeout FROM settings"
            )
        )
    ).all()
    assert execution == (
        [(1, "codex", "openai/gpt-5.5", "api_key", "high", True, 600)] if has_settings else []
    )
    assert await druks_db.scalar(text("SELECT to_regclass('personal_settings')")) is None
    druks_db.expunge_all()
    assert (await InstallationSettings.get()).id == 1


async def test_the_database_refuses_duplicate_installation_settings(druks_db):
    await InstallationSettings.get()
    with pytest.raises(IntegrityError):
        async with druks_db.begin_nested():
            druks_db.add(InstallationSettings(id=2))
            await druks_db.flush()


async def test_the_database_refuses_a_second_default_account(druks_db):
    first = await Account.get_or_create("alice@example.com")
    assert first.is_default
    with pytest.raises(IntegrityError):
        async with druks_db.begin_nested():
            druks_db.add(Account(username="bob@example.com", is_default=True))
            await druks_db.flush()


async def test_run_account_is_the_explicit_account_or_default(druks_db):
    default = await Account.get_or_create("alice@example.com")
    explicit = await Account.get_or_create("bob@example.com")

    assert (await Account.get_for_run(None)).id == default.id
    assert (await Account.get_for_run(explicit.id)).id == explicit.id


async def test_before_setup_execution_refuses_to_create_a_run(druks_db):
    note = await Note.create(body="before setup")
    with pytest.raises(AuthConfigurationError, match="Complete account setup"):
        await _run_instance(Summarize, note.identity)
    assert await druks_db.scalar(select(func.count()).select_from(Run)) == 0


async def test_unattended_subscription_requires_a_connection(druks_db):
    assert await Account.get_default() is None
    with pytest.raises(
        HarnessNotConnectedError, match="connect your Anthropic subscription"
    ) as error:
        await get_config(CONFIG_PROBE.id, None)
    assert error.value.code == "not_connected"


async def test_unattended_api_key_uses_installation_config_without_a_default_account(druks_db):
    account = Account(username="alice@example.com")
    session = db_session()
    session.add(account)
    await session.flush()
    await VaultSecret.paste(Audience.provider("anthropic"), "test-api-key", pasted_by=account)
    installation = await InstallationSettings.get()
    await installation.update(default_billing="api_key", default_effort="low")
    assert await Account.get_default() is None

    config = await get_config(CONFIG_PROBE.id, None)

    assert config.api_key.secrets["value"] == "test-api-key"
    assert config.subscription is None
    assert config.effort == "low"


async def test_accounts_share_execution_defaults_and_keep_their_own_subscriptions(druks_db):
    alice = await connect_anthropic_subscription("alice@example.com")
    bob = await connect_anthropic_subscription("bob@example.com")
    installation = await InstallationSettings.get()
    personal = await Account.get(bob.account_id)
    await personal.update_preferences(timezone="Europe/Madrid")
    await installation.update(default_effort="low", default_timeout=600, fast_mode=True)

    for account_id, subscription in (
        (None, alice),
        (alice.account_id, alice),
        (bob.account_id, bob),
    ):
        config = await get_config(CONFIG_PROBE.id, account_id)
        assert config.subscription.id == subscription.id
        assert (config.effort, config.timeout, config.fast_mode) == ("low", 600, True)
    await SettingsOverride.set_agent_effort(CONFIG_PROBE.id, "high")
    for account_id in (None, alice.account_id, bob.account_id):
        assert (await get_config(CONFIG_PROBE.id, account_id)).effort == "high"


async def test_personal_api_is_scoped_and_does_not_retime_schedules(
    tmp_path, druks_db, monkeypatch
):
    calls = []

    async def apply_schedules():
        calls.append("retimed")

    monkeypatch.setattr("druks.user_settings.routes.apply_schedules", apply_schedules)
    with header_client(tmp_path) as client:
        alice = {IDENTITY_HEADER: "alice@example.com"}
        bob = {IDENTITY_HEADER: "bob@example.com"}
        inherited = client.get("/api/settings/personal", headers=alice).json()
        assert set(inherited) == {"timezone", "gateParkDestinationId"}
        saved = client.patch(
            "/api/settings/personal", headers=alice, json={"timezone": "America/New_York"}
        )
        assert saved.status_code == 200
        account = await Account.get_for_username("alice@example.com")
        assert account.timezone == "America/New_York"
        assert client.get("/api/settings/personal", headers=bob).json()["timezone"] == "UTC"
        assert "timezone" not in client.get("/api/settings", headers=alice).json()
        assert calls == []
        assert (
            client.patch(
                "/api/settings/personal", headers=alice, json={"accountId": "someone-else"}
            ).status_code
            == 422
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("defaultHarness", "codex"),
        ("defaultModel", "openai/gpt-5.5"),
        ("defaultBilling", "api_key"),
        ("defaultEffort", "low"),
        ("fastMode", True),
        ("defaultTimeout", 600),
        ("timezone", "not-a-timezone"),
    ],
)
async def test_invalid_edit_keeps_account_preferences(tmp_path, druks_db, field, value):
    with settings_client(tmp_path) as client:
        response = client.patch("/api/settings/personal", json={field: value})
        assert response.status_code == 422
        assert client.patch("/api/settings/personal", json={}).status_code == 200
    assert (await Account.get_default()).timezone == "UTC"


async def test_shared_agent_overrides_use_installation_settings_after_a_personal_edit(
    tmp_path, druks_db
):
    account = await Account.get_or_create("bob@example.com")
    await account.update_preferences(timezone="Europe/Madrid")

    with settings_client(tmp_path) as client:
        response = client.patch(
            "/api/settings/apps", json={"agentHarnesses": {CONFIG_PROBE.id: "claude"}}
        )

    assert response.status_code == 200
    assert await SettingsOverride.read(f"agent_harness:{CONFIG_PROBE.id}") == "claude"


async def test_notification_default_only_seeds_new_accounts(tmp_path, druks_db):
    destination = await Destination.create(
        name="Default gates", kind="slack_webhook", url="https://example.invalid/hook"
    )
    with header_client(tmp_path) as client:
        alice = {IDENTITY_HEADER: "alice@example.com"}
        bob = {IDENTITY_HEADER: "bob@example.com"}
        charlie = {IDENTITY_HEADER: "charlie@example.com"}
        assert (
            client.get("/api/settings/personal", headers=alice).json()["gateParkDestinationId"]
            is None
        )
        assert (
            client.patch(
                "/api/settings", headers=alice, json={"gateParkDestinationId": destination.id}
            ).status_code
            == 200
        )
        assert (
            client.get("/api/settings/personal", headers=alice).json()["gateParkDestinationId"]
            is None
        )
        assert (
            client.get("/api/settings/personal", headers=bob).json()["gateParkDestinationId"]
            == destination.id
        )
        assert (
            client.patch(
                "/api/settings", headers=alice, json={"gateParkDestinationId": None}
            ).status_code
            == 200
        )
        assert (
            client.get("/api/settings/personal", headers=bob).json()["gateParkDestinationId"]
            == destination.id
        )
        assert (
            client.get("/api/settings/personal", headers=charlie).json()["gateParkDestinationId"]
            is None
        )
