import base64
import json
import shlex
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from conftest import connect_provider, make_jwt
from drukbox_sdk import Secret
from druks.accounts.models import Account
from druks.database import db_session
from druks.harnesses.claude import ClaudeHarness, _get_credentials
from druks.harnesses.codex import CodexHarness
from druks.harnesses.datastructures import SandboxSettings
from druks.harnesses.exceptions import AgentConfigError, HarnessNotConnectedError
from druks.harnesses.opencode import OpenCodeHarness
from druks.harnesses.pi import PiHarness
from druks.harnesses.providers import AnthropicProvider, OpenAiProvider, jwt_claims
from druks.sandbox.datastructures import HomeCopy, HomeFile
from druks.sandbox.models import SandboxIdentity
from druks.secrets.models import VaultSecret
from druks.testing import seed_run
from druks_field_notes.workflows import Summarize


async def _seed_claude(
    *,
    provider_email="op@example.com",
    access="A0",
    refresh="R0",
) -> VaultSecret:
    block = {"accessToken": access, "scopes": ["user:profile"], "subscriptionType": "max"}
    if refresh:
        block["refreshToken"] = refresh
    return await connect_provider(
        AnthropicProvider,
        {"claudeAiOauth": block},
        provider_email=provider_email,
    )


async def test_claude_bundle_carries_no_credential_file(druks_db):
    """The sandbox holds a placeholder for the token. No file carries it."""
    await _seed_claude(access="live", refresh="R0")
    sandbox = SandboxSettings(
        service_url="x",
        service_token="x",
        service_timeout=30.0,
        image="x",
        harness_config_root=Path("/harnesses"),
    )
    bundle = await _get_credentials(sandbox)
    assert not any(type(entry) is HomeFile for entry in bundle.home)
    assert bundle.home[0] == HomeCopy(
        ".claude/settings.json", Path("/harnesses/claude/settings.json")
    )


async def test_the_operators_claude_config_reaches_the_box_without_its_mcp_servers(
    druks_db, tmp_path
):
    # Druks delivers every server it manages; a copied entry could carry a
    # token the vault never saw.
    config_root = tmp_path / "harnesses"
    (config_root / "claude").mkdir(parents=True)
    (config_root / "claude" / ".claude.json").write_text(
        json.dumps(
            {
                "theme": "dark",
                "mcpServers": {"linear": {"headers": {"Authorization": "Bearer lin_secret"}}},
            }
        )
    )
    sandbox = SandboxSettings(
        service_url="x",
        service_token="x",
        service_timeout=30.0,
        image="x",
        harness_config_root=config_root,
    )

    bundle = await _get_credentials(sandbox)

    [config] = [entry for entry in bundle.home if entry.path == ".claude.json"]
    assert json.loads(config.content) == {"theme": "dark"}
    assert "lin_secret" not in repr(bundle)


async def _seed_codex() -> VaultSecret:
    id_token = make_jwt(
        {
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "acc-1",
                "chatgpt_plan_type": "pro",
            },
            "email": "op@example.com",
        }
    )
    return await connect_provider(
        OpenAiProvider,
        {
            "OPENAI_API_KEY": None,
            "tokens": {
                "access_token": make_jwt({"exp": 4_102_444_800}),
                "refresh_token": "rt-secret",
                "id_token": id_token,
                "account_id": "acc-1",
            },
        },
    )


async def test_credentials_builders_read_their_harness_config_directories(druks_db):
    config_root = Path("/harnesses")
    sandbox = SandboxSettings(
        service_url="x",
        service_token="x",
        service_timeout=30.0,
        image="x",
        harness_config_root=config_root,
    )

    claude_bundle = await _get_credentials(sandbox)
    codex_bundle = await CodexHarness(
        model=CodexHarness.default_model,
        fast_mode=False,
        effort=None,
        sandbox=sandbox,
    )._get_credentials(sandbox)

    # No credential file: each CLI reads a placeholder the box holds.
    assert not any(type(entry) is HomeFile for entry in (*claude_bundle.home, *codex_bundle.home))
    assert HomeCopy(".claude/settings.json", config_root / "claude/settings.json") in (
        claude_bundle.home
    )
    assert HomeCopy(".claude/CLAUDE.md", config_root / "claude/CLAUDE.md") in claude_bundle.home
    assert (
        HomeCopy(
            ".claude/plugins/installed_plugins.json",
            config_root / "claude/plugins/installed_plugins.json",
        )
        in claude_bundle.home
    )
    assert (
        HomeCopy(
            ".claude/plugins/known_marketplaces.json",
            config_root / "claude/plugins/known_marketplaces.json",
        )
        in claude_bundle.home
    )
    assert (
        HomeCopy(".claude/plugins/marketplaces", config_root / "claude/plugins/marketplaces")
        in claude_bundle.home
    )
    assert HomeCopy(".claude/plugins/cache", config_root / "claude/plugins/cache") in (
        claude_bundle.home
    )
    assert claude_bundle.home[-1].source == config_root / "claude/skills"
    assert HomeCopy(".codex/config.toml", config_root / "codex/config.toml") in codex_bundle.home
    # MCP credentials are box entries; a copied credentials file would carry a
    # second, unmanaged set.
    assert not any(file.path == ".codex/.credentials.json" for file in codex_bundle.home)
    assert HomeCopy(".codex/AGENTS.md", config_root / "codex/AGENTS.md") in codex_bundle.home
    assert codex_bundle.home[-1].source == config_root / "codex/skills"


@pytest.mark.parametrize(
    ("harness", "config_name", "auth_name"),
    [
        # Each CLI reads its key from a placeholder in the VM, so its invocation
        # carries no key and writes no credential file.
        (ClaudeHarness, "settings.json", ".credentials.json"),
        (CodexHarness, "config.toml", "auth.json"),
    ],
)
@pytest.mark.parametrize("config_exists", [False, True])
async def test_config_delivery_does_not_copy_host_provider_credentials(
    druks_db, tmp_path, harness, config_name, auth_name, config_exists
):
    config_root = tmp_path / "harnesses"
    config_dir = config_root / harness.name
    if config_exists:
        config_dir.mkdir(parents=True)
        (config_dir / config_name).write_text("")
        (config_dir / auth_name).write_text('{"token": "host-token"}')
    sandbox = SandboxSettings(
        service_url="x",
        service_token="x",
        service_timeout=30.0,
        image="x",
        harness_config_root=config_root,
    )

    invocation = await harness(
        model=harness.default_model, fast_mode=False, effort=None, sandbox=sandbox
    ).build_invocation(
        prompt="hello",
        schema={"type": "object"},
        run_id="run-1",
        ssh_username="druks",
    )
    host = AsyncMock()
    for file in invocation.credentials.home:
        await file.push(host, "/home/druks")

    if config_exists:
        host.upload_file.assert_awaited_once_with(
            local=config_dir / config_name,
            remote=f"/home/druks/.{harness.name}/{config_name}",
        )
    else:
        host.upload_file.assert_not_awaited()
    host.upload_dir.assert_not_awaited()
    host.write_secret.assert_not_awaited()
    assert not invocation.env


async def test_a_codex_subscription_binds_a_custom_entry_on_chatgpt(druks_db):
    subscription = await _seed_codex()
    [ref] = CodexHarness.get_secret_refs(subscription)
    await seed_run(db_session(), kind=Summarize.kind, run_id="run-1")

    identity, entries = await SandboxIdentity.create(
        run_id="run-1", scoped_to="workflow", secret_refs=[ref]
    )
    entry = entries["codex_subscription_token"].entry()

    assert ref.key == ("codex_subscription_token", subscription.id, "", "chatgpt.com")
    assert (entry["host"], entry["auth_variable"], entry["auth_header"], entry["auth_prefix"]) == (
        "chatgpt.com",
        "CODEX_SUBSCRIPTION_TOKEN",
        "Authorization",
        "Bearer ",
    )
    assert entry["issuer"]["refresh"] == "1h"
    assert entry["issuer"]["url"].endswith(f"/api/secrets/{identity.id}/codex_subscription_token")


async def test_the_codex_wrapper_writes_its_login_around_the_placeholder(druks_db):
    subscription = await _seed_codex()
    tokens = subscription.secrets["tokens"]
    sandbox = SandboxSettings(
        service_url="x",
        service_token="x",
        service_timeout=30.0,
        image="x",
        harness_config_root=Path("/harnesses"),
    )

    invocation = await CodexHarness(
        model=CodexHarness.default_model, fast_mode=False, effort=None, sandbox=sandbox
    ).build_invocation(
        prompt="hello",
        schema={"type": "object"},
        run_id="run-1",
        ssh_username="druks",
        identity=OpenAiProvider.get_identity(subscription),
    )

    wrapper = invocation.args[2]
    # The file rides in a double-quoted shell word, so the box expands the variable.
    [auth] = [json.loads(word) for word in shlex.split(wrapper) if word.startswith('{"OPENAI')]
    assert auth["OPENAI_API_KEY"] is None
    assert auth["tokens"]["access_token"] == "$CODEX_SUBSCRIPTION_TOKEN"
    assert auth["tokens"]["refresh_token"] == "druks-placeholder"
    assert auth["tokens"]["account_id"] == "acc-1"
    assert auth["last_refresh"].endswith("Z")
    header, _, _ = auth["tokens"]["id_token"].split(".")
    assert json.loads(base64.urlsafe_b64decode(header + "=" * (-len(header) % 4))) == {
        "alg": "none",
        "typ": "JWT",
    }
    assert jwt_claims(auth["tokens"]["id_token"]) == {
        "https://api.openai.com/auth": {"chatgpt_account_id": "acc-1", "chatgpt_plan_type": "pro"},
        "email": "op@example.com",
    }
    assert wrapper.index('chmod 600 "$HOME/.codex/auth.json"') < wrapper.index("codex exec")
    for secret in (tokens["access_token"], tokens["refresh_token"], tokens["id_token"]):
        assert secret not in wrapper
    assert not any(type(entry) is HomeFile for entry in invocation.credentials.home)


_ANTHROPIC_ENTRY = Secret(
    "sk-1",
    host="api.anthropic.com",
    auth_variable="ANTHROPIC_API_KEY",
    auth_header="x-api-key",
    auth_prefix="",
)


def test_key_entries_follow_the_proven_transport_of_each_harness():
    # Codex reads CODEX_API_KEY; every other CLI
    # reads the provider's own variable, x-api-key for Anthropic and a bearer
    # for OpenAI.
    assert CodexHarness.get_secrets("openai", "sk-1") == {
        "openai": Secret(
            "sk-1",
            host="api.openai.com",
            auth_variable="CODEX_API_KEY",
            auth_header="Authorization",
            auth_prefix="Bearer ",
        )
    }
    for harness in (ClaudeHarness, PiHarness, OpenCodeHarness):
        assert harness.get_secrets("anthropic", "sk-1") == {"anthropic": _ANTHROPIC_ENTRY}
    for harness in (PiHarness, OpenCodeHarness):
        assert harness.get_secrets("openai", "sk-1") == {"openai": Secret("sk-1")}


def test_an_unproven_provider_key_refuses_instead_of_entering_the_box():
    with pytest.raises(AgentConfigError, match="'openrouter'"):
        OpenCodeHarness.get_secrets("openrouter", "sk-1")


async def test_credential_without_a_selection_reads_the_accounts_row(druks_db):
    own = await _seed_claude(access="own", provider_email="a@example.com")

    assert (await AnthropicProvider.get_subscription(own.account_id)).id == own.id


async def test_credential_without_any_row_raises(druks_db):
    account = await Account.get_or_create("a@example.com")
    with pytest.raises(HarnessNotConnectedError, match="connect your Anthropic subscription"):
        await AnthropicProvider.get_subscription(account.id)


async def test_credential_for_a_deleted_row_raises(druks_db):
    await _seed_claude(provider_email="a@example.com")  # the surviving fallback
    gone = await _seed_claude(provider_email="b@example.com")
    gone_id = gone.id
    await gone.revoke("user")
    # A disconnect between selection and push fails the call — it must never
    # fall through to another account's payload.
    with pytest.raises(HarnessNotConnectedError, match="removed"):
        await AnthropicProvider.get_subscription(None, subscription_id=gone_id)
