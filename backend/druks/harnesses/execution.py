from dataclasses import dataclass

from druks.accounts.constants import SYSTEM_ACCOUNT_ID
from druks.sandbox.constants import MAX_AGENT_TIMEOUT_SECONDS
from druks.user_settings.models import SettingsOverride, UserSettings

from .base import Harness
from .exceptions import ExecutionSettingsError, HarnessNotConnectedError
from .models import ProviderCatalog, ProviderKey, ProviderSubscription
from .providers import get_provider, is_registered, provider_label
from .registry import get_harness


@dataclass(frozen=True)
class Execution:
    harness_class: type[Harness]
    model: str
    subscription: ProviderSubscription | None
    key: str | None
    effort: str
    timeout: int
    fast_mode: bool

    @property
    def charged_account_id(self) -> str:
        return self.subscription.account_id if self.subscription else SYSTEM_ACCOUNT_ID


async def check_execution(harness_name: str, model: str, billing: str) -> type[Harness]:
    """The harness that runs the triple; a triple no harness runs raises."""
    harness = get_harness(harness_name)
    if not harness:
        raise ExecutionSettingsError(f"no installed harness is named {harness_name!r}.")
    provider_id = model.partition("/")[0]
    if is_registered(provider_id):
        provider = get_provider(provider_id)
        if not harness.has_provider(provider):
            raise ExecutionSettingsError(f"{harness_name} does not run {provider.label} models.")
    else:
        catalog = await ProviderCatalog.get(provider_id)
        if not catalog:
            raise ExecutionSettingsError(
                f"model {model!r} names no provider; add one in Settings → Providers."
            )
        if harness.provider:
            raise ExecutionSettingsError(f"{harness_name} does not run {catalog.label} models.")
        if model not in {entry["id"] for entry in catalog.models}:
            raise ExecutionSettingsError(f"{catalog.label} lists no model {model!r}.")
    if billing not in harness.billing_options:
        raise ExecutionSettingsError(
            f"{harness_name} runs on an API key only; set billing to api_key."
        )
    return harness


async def resolve_execution(agent_name: str, account_id: str | None) -> Execution:
    """How ``agent_name`` runs as ``account_id``, or as the fallback account
    when the run has no actor. A missing credential raises."""
    from druks.apps.registry import agents  # cycle: apps → agents → this module

    agent = agents.get(agent_name)
    if not agent:
        raise KeyError(f"no agent is registered as {agent_name!r}")
    settings = await UserSettings.get()
    harness_name = (await SettingsOverride.agent_harness(agent_name)).value
    model = (await SettingsOverride.agent_model(agent_name)).value
    billing = (await SettingsOverride.agent_billing(agent_name)).value
    harness_class = await check_execution(harness_name, model, billing)
    provider_id = model.partition("/")[0]
    subscription = None
    key = None
    if billing == "api_key":
        provider_key = await ProviderKey.get(provider_id)
        if not provider_key:
            label = await provider_label(provider_id)
            raise HarnessNotConnectedError(f"add the {label} API key in Settings → Providers.")
        key = provider_key.value.decrypt()
    else:
        subscription = await ProviderSubscription.lookup(
            provider_id, account_id or settings.fallback_account_id
        )
    timeout = (await SettingsOverride.agent_timeout(agent_name, agent.timeout)).value
    return Execution(
        harness_class=harness_class,
        model=model,
        subscription=subscription,
        key=key,
        effort=(await SettingsOverride.agent_effort(agent_name)).value,
        # Capped so a single call always fits inside a fresh sandbox lease.
        timeout=min(timeout, MAX_AGENT_TIMEOUT_SECONDS),
        fast_mode=settings.fast_mode,
    )
