import pytest
from conftest import connect_provider
from druks import agents
from druks.accounts.constants import SYSTEM_ACCOUNT_ID
from druks.accounts.models import Account
from druks.harnesses.claude import ClaudeHarness
from druks.harnesses.exceptions import ExecutionSettingsError, HarnessNotConnectedError
from druks.harnesses.execution import check_execution, resolve_execution
from druks.harnesses.models import ProviderCatalog, ProviderKey, ProviderSubscription
from druks.harnesses.opencode import OpenCodeHarness
from druks.harnesses.providers import AnthropicProvider
from druks.sandbox.constants import MAX_AGENT_TIMEOUT_SECONDS
from druks.user_settings.models import SettingsOverride, UserSettings


class _Output(agents.AgentOutput):
    ok: bool


PROBE = agents.Agent(id="execution_probe", prompt="probe.md", contract=_Output)
DECLARED = agents.Agent(id="execution_declared", prompt="probe.md", contract=_Output, timeout=900)
OVERSIZED = agents.Agent(
    id="execution_oversized",
    prompt="probe.md",
    contract=_Output,
    timeout=MAX_AGENT_TIMEOUT_SECONDS * 2,
)


async def test_check_judges_the_triple_together(druks_db):
    assert (
        await check_execution("claude", "anthropic/claude-opus-4-7", "subscription")
        is ClaudeHarness
    )
    assert await check_execution("claude", "anthropic/claude-opus-4-7", "api_key") is ClaudeHarness
    assert (
        await check_execution("opencode", "anthropic/claude-opus-4-7", "api_key") is OpenCodeHarness
    )
    with pytest.raises(ExecutionSettingsError, match="claude does not run OpenAI models"):
        await check_execution("claude", "openai/gpt-5.5", "subscription")
    with pytest.raises(ExecutionSettingsError, match="opencode runs on an API key only"):
        await check_execution("opencode", "anthropic/claude-opus-4-7", "subscription")
    with pytest.raises(ExecutionSettingsError, match="no installed harness is named 'grok'"):
        await check_execution("grok", "anthropic/claude-opus-4-7", "subscription")
    with pytest.raises(ExecutionSettingsError, match="names no provider"):
        await check_execution("claude", "claude-opus-4-7", "subscription")


async def _subscription(email: str) -> ProviderSubscription:
    return await connect_provider(
        AnthropicProvider, {"claudeAiOauth": {"accessToken": email}}, provider_email=email
    )


async def _key() -> ProviderKey:
    return await ProviderKey.create(
        provider="anthropic",
        key="sk-shared",
        account=await Account.get_or_create("ops@example.com"),
    )


async def test_a_subscription_agent_runs_as_its_actor_or_the_fallback_account(druks_db):
    fallback = await _subscription("a@example.com")  # the first login is the fallback
    actor = await _subscription("b@example.com")

    as_actor = await resolve_execution(PROBE.id, actor.account_id)
    unattended = await resolve_execution(PROBE.id, None)

    assert as_actor.subscription.id == actor.id
    assert as_actor.charged_account_id == actor.account_id
    assert unattended.subscription.id == fallback.id
    assert as_actor.key is None
    assert as_actor.harness_class is ClaudeHarness
    assert as_actor.model == "anthropic/claude-opus-4-7"
    assert (as_actor.effort, as_actor.timeout, as_actor.fast_mode) == ("high", 1800, False)


async def test_a_subscription_agent_refuses_without_the_actors_own_subscription(druks_db):
    await _subscription("a@example.com")
    await _key()
    stranger = await Account.get_or_create("stranger@example.com")

    # Neither the fallback account's subscription nor the key stands in.
    with pytest.raises(HarnessNotConnectedError, match="connect your Anthropic subscription"):
        await resolve_execution(PROBE.id, stranger.id)


async def test_a_key_agent_runs_on_the_installations_key_for_anyone(druks_db):
    actor = await _subscription("a@example.com")
    await _key()
    await SettingsOverride.set_agent_billing(PROBE.id, "api_key")

    as_actor = await resolve_execution(PROBE.id, actor.account_id)
    unattended = await resolve_execution(PROBE.id, None)

    assert (as_actor.key, as_actor.subscription) == ("sk-shared", None)
    assert unattended.key == "sk-shared"
    # The key is nobody's, so its calls are charged to the system account.
    assert as_actor.charged_account_id == SYSTEM_ACCOUNT_ID


async def test_a_key_agent_refuses_without_the_key(druks_db):
    actor = await _subscription("a@example.com")
    await SettingsOverride.set_agent_billing(PROBE.id, "api_key")

    with pytest.raises(HarnessNotConnectedError, match="add the Anthropic API key"):
        await resolve_execution(PROBE.id, actor.account_id)


async def test_opencode_runs_an_added_provider_with_its_key_and_model(druks_db):
    await ProviderCatalog.create(
        "openrouter",
        [{"id": "openrouter/anthropic/claude-sonnet-4", "label": "Claude Sonnet 4"}],
        label="OpenRouter",
    )
    await ProviderKey.create(
        provider="openrouter",
        key="sk-openrouter",
        account=await Account.get_or_create("ops@example.com"),
    )
    await SettingsOverride.set_agent_harness(PROBE.id, "opencode")
    await SettingsOverride.set_agent_model(PROBE.id, "openrouter/anthropic/claude-sonnet-4")
    await SettingsOverride.set_agent_billing(PROBE.id, "api_key")

    execution = await resolve_execution(PROBE.id, None)

    assert execution.harness_class is OpenCodeHarness
    assert execution.model == "openrouter/anthropic/claude-sonnet-4"
    assert execution.key == "sk-openrouter"


async def test_an_added_provider_without_a_key_names_it(druks_db):
    await ProviderCatalog.create("groq", [{"id": "groq/llama-4", "label": "Llama 4"}], label="Groq")
    await SettingsOverride.set_agent_harness(PROBE.id, "opencode")
    await SettingsOverride.set_agent_model(PROBE.id, "groq/llama-4")
    await SettingsOverride.set_agent_billing(PROBE.id, "api_key")

    with pytest.raises(HarnessNotConnectedError, match="add the Groq API key in Settings"):
        await resolve_execution(PROBE.id, None)


async def test_an_added_provider_runs_only_on_an_unbound_cli_and_its_own_models(druks_db):
    await ProviderCatalog.create("groq", [{"id": "groq/llama-4", "label": "Llama 4"}], label="Groq")

    with pytest.raises(ExecutionSettingsError, match="claude does not run Groq models"):
        await check_execution("claude", "groq/llama-4", "api_key")
    with pytest.raises(ExecutionSettingsError, match="Groq lists no model 'groq/llama-9'"):
        await check_execution("opencode", "groq/llama-9", "api_key")
    with pytest.raises(ExecutionSettingsError, match="names no provider; add one"):
        await check_execution("opencode", "nobody/model", "api_key")
    assert await check_execution("opencode", "groq/llama-4", "api_key") is OpenCodeHarness


async def test_a_key_only_harness_bills_the_key(druks_db):
    await _subscription("a@example.com")
    await _key()
    await SettingsOverride.set_agent_harness(PROBE.id, "opencode")
    await SettingsOverride.set_agent_billing(PROBE.id, "api_key")

    execution = await resolve_execution(PROBE.id, None)

    assert execution.harness_class is OpenCodeHarness
    assert execution.key == "sk-shared"


async def test_a_stored_triple_no_harness_runs_refuses(druks_db):
    await _subscription("a@example.com")
    await SettingsOverride.set_agent_harness(PROBE.id, "opencode")

    with pytest.raises(ExecutionSettingsError, match="opencode runs on an API key only"):
        await resolve_execution(PROBE.id, None)


async def test_effort_timeout_and_fast_mode_follow_the_defaults_and_overrides(druks_db):
    await _subscription("a@example.com")
    settings = await UserSettings.get()
    await settings.update_profile(default_effort="low", default_timeout=600, fast_mode=True)
    await SettingsOverride.set_agent_effort(DECLARED.id, "medium")

    probe = await resolve_execution(PROBE.id, None)
    declared = await resolve_execution(DECLARED.id, None)
    oversized = await resolve_execution(OVERSIZED.id, None)

    assert (probe.effort, probe.timeout, probe.fast_mode) == ("low", 600, True)
    assert (declared.effort, declared.timeout) == ("medium", 900)
    # Capped so a single call always fits inside a fresh sandbox lease.
    assert oversized.timeout == MAX_AGENT_TIMEOUT_SECONDS


async def test_an_unregistered_agent_is_named(druks_db):
    with pytest.raises(KeyError, match="no agent is registered as 'ghost'"):
        await resolve_execution("ghost", None)
