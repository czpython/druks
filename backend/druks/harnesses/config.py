from dataclasses import dataclass

from drukbox_sdk import Secret

from druks.accounts.models import Account
from druks.sandbox.constants import MAX_AGENT_TIMEOUT_SECONDS
from druks.sandbox.models import SecretRef
from druks.secrets.datastructures import Audience
from druks.secrets.enums import SecretKind
from druks.secrets.models import VaultSecret
from druks.user_settings.models import InstallationSettings, SettingsOverride

from .base import Harness
from .exceptions import AgentConfigError, HarnessNotConnectedError
from .models import ProviderCatalog
from .providers import get_provider, is_registered, provider_label
from .registry import get_harness


@dataclass(frozen=True)
class AgentConfig:
    """Shared execution settings with the account's selected credential."""

    harness_class: type[Harness]
    model: str
    subscription: VaultSecret | None
    api_key: VaultSecret | None
    secrets: dict[str, Secret]
    # The secrets a box fetches through the issuer, beyond its pasted key.
    secret_refs: list[SecretRef]
    # The subscription's non-secret facts, for the login a box sees. Empty for a key.
    identity: dict
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
    def secrets_id(self) -> str:
        """What a box created for this config holds: the pasted key, or the
        subscriptions it fetches."""
        if self.secrets:
            return f"{self.api_key.audience_name}.{self.api_key.updated_at:%Y%m%dT%H%M%S}"
        return ".".join(ref.secret_id for ref in self.secret_refs)

    @property
    def charged_account_id(self) -> str | None:
        return self.subscription.account_id if self.subscription else None


async def check_config(harness_name: str, model: str, billing: str) -> type[Harness]:
    """The harness that runs the triple; a triple no harness runs raises."""
    harness = get_harness(harness_name)
    if not harness:
        raise AgentConfigError(f"no installed harness is named {harness_name!r}.")
    provider_id = model.partition("/")[0]
    if is_registered(provider_id):
        provider = get_provider(provider_id)
        if not harness.has_provider(provider):
            raise AgentConfigError(f"{harness_name} does not run {provider.label} models.")
    else:
        catalog = await ProviderCatalog.get(provider_id)
        if not catalog:
            raise AgentConfigError(
                f"model {model!r} names no provider; add one in Settings → Providers."
            )
        if harness.provider:
            raise AgentConfigError(f"{harness_name} does not run {catalog.label} models.")
        if model not in {entry["id"] for entry in catalog.models}:
            raise AgentConfigError(f"{catalog.label} lists no model {model!r}.")
    if billing not in harness.billing_options:
        raise AgentConfigError(f"{harness_name} runs on an API key only; set billing to api_key.")
    return harness


async def get_config(agent_name: str, account_id: str | None) -> AgentConfig:
    """Resolve shared execution settings and the supplied or default account's credential.
    A missing credential raises."""
    from druks.apps.registry import agents  # cycle: apps → agents → this module

    agent = agents.get(agent_name)
    if not agent:
        raise KeyError(f"no agent is registered as {agent_name!r}")
    if not account_id:
        account = await Account.get_default()
        account_id = account.id if account else None
    settings = await InstallationSettings.get()
    harness_name = (await SettingsOverride.agent_harness(agent_name, settings=settings)).value
    model = (await SettingsOverride.agent_model(agent_name, settings=settings)).value
    billing = (await SettingsOverride.agent_billing(agent_name, settings=settings)).value
    harness_class = await check_config(harness_name, model, billing)
    provider_id = model.partition("/")[0]
    subscription = None
    provider_key = None
    secrets: dict[str, Secret] = {}
    secret_refs: list[SecretRef] = []
    identity: dict = {}
    if billing == "api_key":
        provider_key = await VaultSecret.lookup(SecretKind.STATIC, Audience.provider(provider_id))
        if not provider_key:
            label = await provider_label(provider_id)
            raise HarnessNotConnectedError(f"add the {label} API key in Settings → Providers.")
        secrets = harness_class.get_secrets(provider_id, provider_key.secrets["value"])
    else:
        provider = get_provider(provider_id)
        subscription = await provider.get_subscription(account_id)
        identity = provider.get_identity(subscription)
        secret_refs = harness_class.get_secret_refs(subscription)
    timeout = (
        await SettingsOverride.agent_timeout(agent_name, agent.timeout, settings=settings)
    ).value
    return AgentConfig(
        harness_class=harness_class,
        model=model,
        subscription=subscription,
        api_key=provider_key,
        secrets=secrets,
        secret_refs=secret_refs,
        identity=identity,
        billing=billing,
        effort=(await SettingsOverride.agent_effort(agent_name, settings=settings)).value,
        # Capped so a single call always fits inside a fresh sandbox lease.
        timeout=min(timeout, MAX_AGENT_TIMEOUT_SECONDS),
        fast_mode=settings.fast_mode,
    )
