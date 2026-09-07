from dataclasses import dataclass

from druks.accounts.models import Account
from druks.sandbox.constants import MAX_AGENT_TIMEOUT_SECONDS
from druks.user_settings.models import SettingsOverride, SettingsProfile

from .base import Harness
from .exceptions import HarnessNotConnectedError, ProfileSettingsError
from .models import ProviderCatalog, ProviderKey, ProviderSubscription
from .providers import get_provider, is_registered, provider_label
from .registry import get_harness


@dataclass(frozen=True)
class Profile:
    """How an agent runs for one account: harness, model, effort, billing, read from
    Settings → Agents at call time. ``key`` rides here because an app driving its
    own CLI in the VM builds that CLI's environment; the harness fills only the
    calling agent's."""

    harness_class: type[Harness]
    model: str
    subscription: ProviderSubscription | None
    api_key: ProviderKey | None
    billing: str
    effort: str
    timeout: int
    fast_mode: bool

    @property
    def harness(self) -> str:
        return self.harness_class.name

    @property
    def model_id(self) -> str:
        """The model as the CLI names it, without the provider namespace."""
        return self.model.partition("/")[2]

    @property
    def key(self) -> str | None:
        return self.api_key.value.decrypt() if self.api_key else None

    @property
    def charged_account_id(self) -> str | None:
        return self.subscription.account_id if self.subscription else None


async def check_profile(harness_name: str, model: str, billing: str) -> type[Harness]:
    """The harness that runs the triple; a triple no harness runs raises."""
    harness = get_harness(harness_name)
    if not harness:
        raise ProfileSettingsError(f"no installed harness is named {harness_name!r}.")
    provider_id = model.partition("/")[0]
    if is_registered(provider_id):
        provider = get_provider(provider_id)
        if not harness.has_provider(provider):
            raise ProfileSettingsError(f"{harness_name} does not run {provider.label} models.")
    else:
        catalog = await ProviderCatalog.get(provider_id)
        if not catalog:
            raise ProfileSettingsError(
                f"model {model!r} names no provider; add one in Settings → Providers."
            )
        if harness.provider:
            raise ProfileSettingsError(f"{harness_name} does not run {catalog.label} models.")
        if model not in {entry["id"] for entry in catalog.models}:
            raise ProfileSettingsError(f"{catalog.label} lists no model {model!r}.")
    if billing not in harness.billing_options:
        raise ProfileSettingsError(
            f"{harness_name} runs on an API key only; set billing to api_key."
        )
    return harness


async def get_profile(agent_name: str, account_id: str | None) -> Profile:
    """Resolve an agent's profile for the supplied or default account.
    A missing credential raises."""
    from druks.apps.registry import agents  # cycle: apps → agents → this module

    agent = agents.get(agent_name)
    if not agent:
        raise KeyError(f"no agent is registered as {agent_name!r}")
    if not account_id:
        account = await Account.get_default()
        account_id = account.id if account else None
    settings = await SettingsProfile.get(account_id)
    harness_name = (await SettingsOverride.agent_harness(agent_name, settings=settings)).value
    model = (await SettingsOverride.agent_model(agent_name, settings=settings)).value
    billing = (await SettingsOverride.agent_billing(agent_name, settings=settings)).value
    harness_class = await check_profile(harness_name, model, billing)
    provider_id = model.partition("/")[0]
    subscription = None
    provider_key = None
    if billing == "api_key":
        provider_key = await ProviderKey.get(provider_id)
        if not provider_key:
            label = await provider_label(provider_id)
            raise HarnessNotConnectedError(f"add the {label} API key in Settings → Providers.")
    else:
        subscription = await ProviderSubscription.lookup(provider_id, account_id)
    timeout = (
        await SettingsOverride.agent_timeout(agent_name, agent.timeout, settings=settings)
    ).value
    return Profile(
        harness_class=harness_class,
        model=model,
        subscription=subscription,
        api_key=provider_key,
        billing=billing,
        effort=(await SettingsOverride.agent_effort(agent_name, settings=settings)).value,
        # Capped so a single call always fits inside a fresh sandbox lease.
        timeout=min(timeout, MAX_AGENT_TIMEOUT_SECONDS),
        fast_mode=settings.fast_mode,
    )
