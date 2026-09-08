from types import SimpleNamespace

import pytest
from conftest import PROFILE_PROBE, ProfileOutput, connect_anthropic_subscription
from drukbox_sdk import Secret
from druks import agents
from druks.accounts.models import Account
from druks.apps import App
from druks.apps.registry import agents as agent_registry
from druks.database import db_session
from druks.durable.models import AgentCall
from druks.harnesses.claude import ClaudeHarness
from druks.harnesses.exceptions import HarnessNotConnectedError, ProfileSettingsError
from druks.harnesses.models import ProviderCatalog
from druks.harnesses.opencode import OpenCodeHarness
from druks.harnesses.profiles import check_profile, get_profile
from druks.sandbox.constants import MAX_AGENT_TIMEOUT_SECONDS
from druks.secrets.datastructures import Audience
from druks.secrets.enums import SecretKind
from druks.secrets.models import VaultSecret
from druks.testing import seed_call, seed_run
from druks.user_settings import reads
from druks.user_settings.models import SettingsOverride, SettingsProfile
from druks.workflows import WorkflowError, current_workflow
from druks_field_notes.workflows import Summarize
from sqlalchemy.exc import IntegrityError

DECLARED = agents.Agent(
    id="profile_declared", prompt="probe.md", contract=ProfileOutput, timeout=900
)
OVERSIZED = agents.Agent(
    id="profile_oversized",
    prompt="probe.md",
    contract=ProfileOutput,
    timeout=MAX_AGENT_TIMEOUT_SECONDS * 2,
)


async def test_check_judges_the_triple_together(druks_db):
    assert (
        await check_profile("claude", "anthropic/claude-opus-4-7", "subscription") is ClaudeHarness
    )
    assert await check_profile("claude", "anthropic/claude-opus-4-7", "api_key") is ClaudeHarness
    assert (
        await check_profile("opencode", "anthropic/claude-opus-4-7", "api_key") is OpenCodeHarness
    )
    with pytest.raises(ProfileSettingsError, match="claude does not run OpenAI models"):
        await check_profile("claude", "openai/gpt-5.5", "subscription")
    with pytest.raises(ProfileSettingsError, match="opencode runs on an API key only"):
        await check_profile("opencode", "anthropic/claude-opus-4-7", "subscription")
    with pytest.raises(ProfileSettingsError, match="no installed harness is named 'grok'"):
        await check_profile("grok", "anthropic/claude-opus-4-7", "subscription")
    with pytest.raises(ProfileSettingsError, match="names no provider"):
        await check_profile("claude", "claude-opus-4-7", "subscription")


async def _key() -> VaultSecret:
    return await VaultSecret.paste(
        Audience.provider("anthropic"),
        "sk-shared",
        pasted_by=await Account.get_or_create("ops@example.com"),
    )


_SHARED_ENTRY = Secret(
    "sk-shared",
    host="api.anthropic.com",
    auth_variable="ANTHROPIC_API_KEY",
    auth_header="x-api-key",
    auth_prefix="",
)


@pytest.mark.parametrize("billing", ["subscription", "api_key"])
async def test_call_keeps_its_billing_reference_after_disconnect(druks_db, billing):
    subscription = await connect_anthropic_subscription("a@example.com")
    key = await _key()
    run = await seed_run(druks_db, kind=Summarize.kind, account_id=subscription.account_id)
    call = await seed_call(
        druks_db,
        run,
        PROFILE_PROBE.id,
        subscription_id=subscription.id if billing == "subscription" else None,
        api_key_id=key.id if billing == "api_key" else None,
    )

    if billing == "subscription":
        await subscription.revoke("user")
        await subscription.update_secrets({"late_refresh": "secret"}, expires_at=None)
        assert not dict(subscription.secrets)
        assert not subscription.is_live
        assert await type(subscription).reload(subscription.id) is None
        assert call.subscription_id == subscription.id
        connected = await connect_anthropic_subscription("a@example.com")
        assert connected.id == subscription.id
    else:
        await key.revoke("user")
        await db_session().refresh(key)
        assert dict(key.secrets) == {}
        assert await VaultSecret.lookup(SecretKind.STATIC, key.audience) is None
        assert call.api_key_id == key.id
        assert (await _key()).id == key.id

    assert (await AgentCall.get(call.id)).id == call.id


@pytest.mark.parametrize("both", [False, True])
async def test_call_requires_exactly_one_billing_reference(druks_db, both):
    subscription = await connect_anthropic_subscription("a@example.com")
    key = await _key()
    run = await seed_run(druks_db, kind=Summarize.kind)
    with pytest.raises(IntegrityError):
        async with druks_db.begin_nested():
            druks_db.add(
                AgentCall(
                    run_id=run.id,
                    agent=PROFILE_PROBE.id,
                    model="anthropic/claude-opus-4-7",
                    sandbox_host_id="test-host",
                    subscription_id=subscription.id if both else None,
                    api_key_id=key.id if both else None,
                )
            )
            await druks_db.flush()


async def test_a_subscription_agent_runs_as_its_actor_or_the_default_account(druks_db):
    default_subscription = await connect_anthropic_subscription("a@example.com")
    actor = await connect_anthropic_subscription("b@example.com")

    as_actor = await get_profile(PROFILE_PROBE.id, actor.account_id)
    unattended = await get_profile(PROFILE_PROBE.id, None)

    assert as_actor.subscription.id == actor.id
    assert as_actor.charged_account_id == actor.account_id
    assert unattended.subscription.id == default_subscription.id
    assert (as_actor.secrets, as_actor.secrets_id, as_actor.key) == ({}, actor.id, None)
    [secret] = as_actor.secret_refs
    assert secret.key == ("anthropic", actor.id, "")
    assert as_actor.harness_class is ClaudeHarness
    assert as_actor.model == "anthropic/claude-opus-4-7"
    assert (as_actor.effort, as_actor.timeout, as_actor.fast_mode) == ("high", 1800, False)


async def test_a_subscription_agent_refuses_without_the_actors_own_subscription(druks_db):
    await connect_anthropic_subscription("a@example.com")
    await _key()
    stranger = await Account.get_or_create("stranger@example.com")

    # A missing personal subscription cannot borrow another credential.
    with pytest.raises(HarnessNotConnectedError, match="connect your Anthropic subscription"):
        await get_profile(PROFILE_PROBE.id, stranger.id)


async def test_a_key_agent_runs_on_the_installations_key_for_anyone(druks_db):
    actor = await connect_anthropic_subscription("a@example.com")
    pasted = await _key()
    await SettingsOverride.set_agent_billing(PROFILE_PROBE.id, "api_key")

    as_actor = await get_profile(PROFILE_PROBE.id, actor.account_id)
    unattended = await get_profile(PROFILE_PROBE.id, None)

    # Claude reads the key from a placeholder in the VM, never from its invocation.
    assert (as_actor.secrets, as_actor.key, as_actor.subscription) == (
        {"anthropic": _SHARED_ENTRY},
        None,
        None,
    )
    assert unattended.secrets == {"anthropic": _SHARED_ENTRY}
    # The entries' identity is the pasted key, with no secret material.
    assert as_actor.secrets_id == f"anthropic.{pasted.updated_at:%Y%m%dT%H%M%S}"
    assert "sk-shared" not in as_actor.secrets_id
    # The key is nobody's, so its calls are charged to the installation.
    assert as_actor.charged_account_id is None


async def test_a_key_agent_refuses_without_the_key(druks_db):
    actor = await connect_anthropic_subscription("a@example.com")
    await SettingsOverride.set_agent_billing(PROFILE_PROBE.id, "api_key")

    with pytest.raises(HarnessNotConnectedError, match="add the Anthropic API key"):
        await get_profile(PROFILE_PROBE.id, actor.account_id)


async def test_opencode_runs_an_added_provider_with_its_key_and_model(druks_db):
    await ProviderCatalog.create(
        "openrouter",
        [{"id": "openrouter/anthropic/claude-sonnet-4", "label": "Claude Sonnet 4"}],
        label="OpenRouter",
    )
    await VaultSecret.paste(
        Audience.provider("openrouter"),
        "sk-openrouter",
        pasted_by=await Account.get_or_create("ops@example.com"),
    )
    await SettingsOverride.set_agent_harness(PROFILE_PROBE.id, "opencode")
    await SettingsOverride.set_agent_model(PROFILE_PROBE.id, "openrouter/anthropic/claude-sonnet-4")
    await SettingsOverride.set_agent_billing(PROFILE_PROBE.id, "api_key")

    profile = await get_profile(PROFILE_PROBE.id, None)

    assert profile.harness_class is OpenCodeHarness
    assert profile.model == "openrouter/anthropic/claude-sonnet-4"
    # OpenCode still reads the key from its invocation.
    assert (profile.secrets, profile.key) == ({}, "sk-openrouter")


async def test_an_added_provider_without_a_key_names_it(druks_db):
    await Account.get_or_create("ops@example.com")
    await ProviderCatalog.create("groq", [{"id": "groq/llama-4", "label": "Llama 4"}], label="Groq")
    await SettingsOverride.set_agent_harness(PROFILE_PROBE.id, "opencode")
    await SettingsOverride.set_agent_model(PROFILE_PROBE.id, "groq/llama-4")
    await SettingsOverride.set_agent_billing(PROFILE_PROBE.id, "api_key")

    with pytest.raises(HarnessNotConnectedError, match="add the Groq API key in Settings"):
        await get_profile(PROFILE_PROBE.id, None)


async def test_an_added_provider_runs_only_on_an_unbound_cli_and_its_own_models(druks_db):
    await Account.get_or_create("ops@example.com")
    await ProviderCatalog.create("groq", [{"id": "groq/llama-4", "label": "Llama 4"}], label="Groq")

    with pytest.raises(ProfileSettingsError, match="claude does not run Groq models"):
        await check_profile("claude", "groq/llama-4", "api_key")
    with pytest.raises(ProfileSettingsError, match="Groq lists no model 'groq/llama-9'"):
        await check_profile("opencode", "groq/llama-9", "api_key")
    with pytest.raises(ProfileSettingsError, match="names no provider; add one"):
        await check_profile("opencode", "nobody/model", "api_key")
    assert await check_profile("opencode", "groq/llama-4", "api_key") is OpenCodeHarness


async def test_a_key_only_harness_bills_the_key(druks_db):
    await connect_anthropic_subscription("a@example.com")
    await _key()
    await SettingsOverride.set_agent_harness(PROFILE_PROBE.id, "opencode")
    await SettingsOverride.set_agent_billing(PROFILE_PROBE.id, "api_key")

    profile = await get_profile(PROFILE_PROBE.id, None)

    assert profile.harness_class is OpenCodeHarness
    assert (profile.secrets, profile.key) == ({}, "sk-shared")


async def test_a_stored_triple_no_harness_runs_refuses(druks_db):
    await connect_anthropic_subscription("a@example.com")
    await SettingsOverride.set_agent_harness(PROFILE_PROBE.id, "opencode")

    with pytest.raises(ProfileSettingsError, match="opencode runs on an API key only"):
        await get_profile(PROFILE_PROBE.id, None)


async def test_effort_timeout_and_fast_mode_follow_the_defaults_and_overrides(druks_db):
    await connect_anthropic_subscription("a@example.com")
    settings = await SettingsProfile.get()
    await settings.update_profile(default_effort="low", default_timeout=600, fast_mode=True)
    await SettingsOverride.set_agent_effort(DECLARED.id, "medium")

    probe = await get_profile(PROFILE_PROBE.id, None)
    declared = await get_profile(DECLARED.id, None)
    oversized = await get_profile(OVERSIZED.id, None)

    assert (probe.effort, probe.timeout, probe.fast_mode) == ("low", 600, True)
    assert (declared.effort, declared.timeout) == ("medium", 900)
    # Capped so a single call always fits inside a fresh sandbox lease.
    assert oversized.timeout == MAX_AGENT_TIMEOUT_SECONDS


async def test_an_agent_reads_its_own_profile(druks_db):
    await connect_anthropic_subscription("a@example.com")
    await _key()

    token = current_workflow.set(SimpleNamespace(account_id=None))
    subscribed = await PROFILE_PROBE.get_profile()
    await SettingsOverride.set_agent_billing(PROFILE_PROBE.id, "api_key")
    keyed = await PROFILE_PROBE.get_profile()
    current_workflow.reset(token)
    with pytest.raises(WorkflowError, match="only inside a workflow"):
        await PROFILE_PROBE.get_profile()

    assert (subscribed.harness, subscribed.model_id) == ("claude", "claude-opus-4-7")
    assert (subscribed.billing, subscribed.secrets, subscribed.key) == ("subscription", {}, None)
    assert (keyed.billing, keyed.secrets, keyed.key) == (
        "api_key",
        {"anthropic": _SHARED_ENTRY},
        None,
    )


async def test_an_unregistered_agent_is_named(druks_db):
    with pytest.raises(KeyError, match="no agent is registered as 'ghost'"):
        await get_profile("ghost", None)


async def test_two_apps_declare_the_same_agent_name(druks_db):
    class Ticketing(App):
        name = "ticketing"
        file_tickets = agents.Agent(prompt="probe.md", contract=ProfileOutput)

    class BugHunter(App):
        name = "bug_hunter"
        file_tickets = agents.Agent(
            name="File tickets", prompt="probe.md", contract=ProfileOutput, timeout=300
        )

    try:
        await connect_anthropic_subscription("a@example.com")
        await SettingsOverride.set_agent_effort(BugHunter.file_tickets.id, "low")
        declared = (Ticketing.agents(), BugHunter.agents())
        ticketing = await get_profile(Ticketing.file_tickets.id, None)
        bug_hunter = await get_profile(BugHunter.file_tickets.id, None)
        settings = [
            await reads.get_agent_setting(agent, settings=await SettingsProfile.get())
            for agent in (Ticketing.file_tickets, BugHunter.file_tickets)
        ]
    finally:
        for app in (Ticketing, BugHunter):
            agent_registry._items.pop(app.file_tickets.id)

    assert (Ticketing.file_tickets.id, BugHunter.file_tickets.id) == (
        "ticketing.file_tickets",
        "bug_hunter.file_tickets",
    )
    assert declared == ([Ticketing.file_tickets], [BugHunter.file_tickets])
    assert (ticketing.effort, ticketing.timeout) == ("high", 1800)
    assert (bug_hunter.effort, bug_hunter.timeout) == ("low", 300)
    # The settings wire keys on the id; the declared name is only its label.
    assert [(setting.name, setting.label, setting.effort) for setting in settings] == [
        ("ticketing.file_tickets", "file_tickets", "high"),
        ("bug_hunter.file_tickets", "File tickets", "low"),
    ]
