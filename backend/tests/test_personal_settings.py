import pytest
from conftest import (
    IDENTITY_HEADER,
    PROFILE_PROBE,
    connect_anthropic_subscription,
    header_client,
    settings_client,
)
from druks.accounts.exceptions import AuthConfigurationError
from druks.accounts.models import Account
from druks.database import db_session
from druks.durable.models import Run
from druks.harnesses.exceptions import HarnessNotConnectedError
from druks.harnesses.models import ProviderKey
from druks.harnesses.profiles import get_profile
from druks.user_settings.models import SettingsOverride, SettingsProfile
from druks.workflows import _run_instance
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError


async def test_personal_reads_inherit_without_creating_a_row(druks_db):
    account = await Account.get_or_create("alice@example.com")
    installation = await SettingsProfile.get()
    await installation.update_profile(timezone="Europe/Madrid", default_effort="low")

    personal = await SettingsProfile.get(account.id)

    assert personal.account_id is None
    assert personal.timezone == "Europe/Madrid"
    assert await druks_db.scalar(select(func.count()).select_from(SettingsProfile)) == 1


async def test_first_edit_copies_the_profile_and_later_defaults_do_not_change_it(druks_db):
    alice = await Account.get_or_create("alice@example.com")
    bob = await Account.get_or_create("bob@example.com")
    installation = await SettingsProfile.get()
    await installation.update_profile(
        timezone="Europe/Madrid", default_effort="low", fast_mode=True
    )
    personal = await installation.copy_for_account(alice.id)
    await personal.update_profile(timezone="America/New_York")
    await installation.update_profile(default_effort="high", fast_mode=False)

    assert (await SettingsProfile.get(alice.id)).default_effort == "low"
    assert personal.fast_mode
    assert personal.timezone == "America/New_York"
    assert (await SettingsProfile.get(bob.id)).default_effort == "high"
    assert await druks_db.scalar(select(func.count()).select_from(SettingsProfile)) == 2
    existing_profile = await installation.copy_for_account(alice.id)
    assert existing_profile.id == personal.id
    assert existing_profile.default_effort == "low"


async def test_the_database_refuses_duplicate_installation_or_personal_profiles(druks_db):
    account = await Account.get_or_create("alice@example.com")
    installation = await SettingsProfile.get()
    await installation.copy_for_account(account.id)
    for account_id in (None, account.id):
        with pytest.raises(IntegrityError):
            async with druks_db.begin_nested():
                druks_db.add(SettingsProfile(account_id=account_id))
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
        await get_profile(PROFILE_PROBE.id, None)
    assert error.value.code == "not_connected"


async def test_unattended_api_key_uses_installation_profile_without_a_default_account(druks_db):
    account = Account(username="alice@example.com")
    session = db_session()
    session.add(account)
    await session.flush()
    await ProviderKey.create(provider="anthropic", key="test-api-key", account=account)
    installation = await SettingsProfile.get()
    await installation.update_profile(default_billing="api_key", default_effort="low")
    assert await Account.get_default() is None

    profile = await get_profile(PROFILE_PROBE.id, None)

    assert profile.key == "test-api-key"
    assert profile.subscription is None
    assert profile.effort == "low"


async def test_unattended_execution_uses_the_default_personal_profile_and_agent_overrides(druks_db):
    subscription = await connect_anthropic_subscription("alice@example.com")
    installation = await SettingsProfile.get()
    personal = await installation.copy_for_account(subscription.account_id)
    await personal.update_profile(default_effort="low", default_timeout=600, fast_mode=True)

    profile = await get_profile(PROFILE_PROBE.id, None)

    assert profile.subscription.id == subscription.id
    assert (profile.effort, profile.timeout, profile.fast_mode) == ("low", 600, True)
    await SettingsOverride.set_agent_effort(PROFILE_PROBE.id, "high")
    assert (await get_profile(PROFILE_PROBE.id, None)).effort == "high"


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
        assert (
            client.patch(
                "/api/settings", headers=alice, json={"timezone": "Europe/Madrid"}
            ).status_code
            == 200
        )
        inherited = client.get("/api/settings/personal", headers=alice).json()
        assert inherited["accountId"] is None
        saved = client.patch(
            "/api/settings/personal", headers=alice, json={"timezone": "America/New_York"}
        )
        assert saved.status_code == 200
        assert saved.json()["accountId"] == (await Account.get_for_username("alice@example.com")).id
        assert (
            client.get("/api/settings/personal", headers=bob).json()["timezone"] == "Europe/Madrid"
        )
        assert client.get("/api/settings", headers=alice).json()["timezone"] == "Europe/Madrid"
        assert calls == ["retimed"]
        assert (
            client.patch(
                "/api/settings/personal", headers=alice, json={"accountId": "someone-else"}
            ).status_code
            == 422
        )


async def test_invalid_first_edit_leaves_no_personal_row(tmp_path, druks_db):
    with settings_client(tmp_path) as client:
        response = client.patch("/api/settings/personal", json={"defaultModel": "openai/gpt-5.5"})
        assert response.status_code == 422
        assert client.get("/api/settings/personal").json()["accountId"] is None
        assert client.patch("/api/settings/personal", json={}).json()["accountId"] is None


async def test_shared_agent_overrides_must_fit_other_accounts_profiles(tmp_path, druks_db):
    account = await Account.get_or_create("bob@example.com")
    installation = await SettingsProfile.get()
    personal = await installation.copy_for_account(account.id)
    await personal.update_profile(default_harness="codex", default_model="openai/gpt-5.5")

    with settings_client(tmp_path) as client:
        response = client.patch(
            "/api/settings/apps", json={"agentHarnesses": {PROFILE_PROBE.id: "claude"}}
        )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "Personal profile for bob@example.com: claude does not run OpenAI models."
    )
    assert await SettingsOverride.read(f"agent_harness:{PROFILE_PROBE.id}") is None
