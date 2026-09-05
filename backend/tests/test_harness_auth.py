import base64
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from conftest import connect_provider
from druks.accounts.models import Account
from druks.harnesses.claude import ClaudeHarness, _get_credentials
from druks.harnesses.codex import CodexHarness
from druks.harnesses.datastructures import SandboxSettings
from druks.harnesses.exceptions import HarnessNotConnectedError
from druks.harnesses.models import ProviderSubscription
from druks.harnesses.providers import AnthropicProvider, OpenAiProvider
from druks.sandbox.datastructures import HomeCopy


async def _seed_claude(
    *,
    provider_email="op@example.com",
    access="A0",
    refresh="R0",
) -> ProviderSubscription:
    block = {"accessToken": access, "scopes": ["user:profile"], "subscriptionType": "max"}
    if refresh:
        block["refreshToken"] = refresh
    return await connect_provider(
        AnthropicProvider,
        {"claudeAiOauth": block},
        provider_email=provider_email,
    )


async def test_claude_builder_puts_db_credentials_on_the_bundle(druks_db):
    subscription = await _seed_claude(access="live", refresh="R0")
    sandbox = SandboxSettings(
        service_url="x",
        service_token="x",
        service_timeout=30.0,
        image="x",
        harness_config_root=Path("/harnesses"),
    )
    bundle = await _get_credentials(sandbox, github_token=None, subscription=subscription)
    auth = bundle.home[0]
    assert auth.path == ".claude/.credentials.json"
    assert json.loads(auth.content)["claudeAiOauth"]["accessToken"] == "live"


async def test_credentials_builders_read_their_harness_config_directories(druks_db):
    claude_subscription = await _seed_claude()
    far_future_expiration = 4_102_444_800
    jwt_header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    jwt_payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": far_future_expiration}).encode())
        .rstrip(b"=")
        .decode()
    )
    codex_subscription = await connect_provider(
        OpenAiProvider,
        {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "access_token": f"{jwt_header}.{jwt_payload}.sig",
                "refresh_token": "R0",
                "account_id": "acc-1",
            },
        },
    )
    config_root = Path("/harnesses")
    sandbox = SandboxSettings(
        service_url="x",
        service_token="x",
        service_timeout=30.0,
        image="x",
        harness_config_root=config_root,
    )

    claude_bundle = await _get_credentials(
        sandbox,
        github_token=None,
        subscription=claude_subscription,
    )
    codex_bundle = await CodexHarness(
        model=CodexHarness.default_model,
        fast_mode=False,
        effort=None,
        sandbox=sandbox,
    )._get_credentials(
        sandbox,
        github_token=None,
        subscription=codex_subscription,
        key=None,
    )

    assert claude_bundle.home[0].path == ".claude/.credentials.json"
    assert codex_bundle.home[0].path == ".codex/auth.json"
    assert HomeCopy(".claude/settings.json", config_root / "claude/settings.json") in (
        claude_bundle.home
    )
    assert HomeCopy(".claude/CLAUDE.md", config_root / "claude/CLAUDE.md") in claude_bundle.home
    assert HomeCopy(".claude.json", config_root / "claude/.claude.json") in claude_bundle.home
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
    assert (
        HomeCopy(".codex/.credentials.json", config_root / "codex/.credentials.json")
        in codex_bundle.home
    )
    assert HomeCopy(".codex/AGENTS.md", config_root / "codex/AGENTS.md") in codex_bundle.home
    assert codex_bundle.home[-1].source == config_root / "codex/skills"


async def test_missing_config_root_keeps_the_db_credential(druks_db, tmp_path):
    subscription = await _seed_claude(access="live")
    sandbox = SandboxSettings(
        service_url="x",
        service_token="x",
        service_timeout=30.0,
        image="x",
        harness_config_root=tmp_path / "missing",
    )
    bundle = await _get_credentials(sandbox, github_token="gh", subscription=subscription)
    auth = bundle.home[0]
    assert json.loads(auth.content)["claudeAiOauth"]["accessToken"] == "live"
    assert bundle.github_token == "gh"


@pytest.mark.parametrize(
    ("harness", "config_name", "auth_name"),
    [
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
        key="selected-key",
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
    if harness.name == "codex":
        host.write_secret.assert_awaited_once_with(
            secret=json.dumps({"OPENAI_API_KEY": "selected-key"}),
            remote="/home/druks/.codex/auth.json",
        )
    else:
        host.write_secret.assert_not_awaited()
        assert invocation.env["ANTHROPIC_API_KEY"] == "selected-key"


async def test_credential_without_a_selection_reads_the_accounts_row(druks_db):
    own = await _seed_claude(access="own", provider_email="a@example.com")

    assert (await ProviderSubscription.lookup("anthropic", own.account_id)).id == own.id


async def test_credential_without_any_row_raises(druks_db):
    account = await Account.get_or_create("a@example.com")
    with pytest.raises(HarnessNotConnectedError, match="connect your Anthropic subscription"):
        await ProviderSubscription.lookup("anthropic", account.id)


async def test_credential_renders_only_the_selected_row(druks_db):
    mine = await _seed_claude(access="mine-token", provider_email="a@example.com")
    other = await _seed_claude(access="other-token", provider_email="b@example.com")

    selected = await ProviderSubscription.lookup("anthropic", None, subscription_id=other.id)
    rendered = json.loads(ClaudeHarness.auth_file(selected).content)
    assert rendered["claudeAiOauth"]["accessToken"] == "other-token"
    assert "mine-token" not in json.dumps(rendered)
    selected = await ProviderSubscription.lookup("anthropic", None, subscription_id=mine.id)
    rendered = json.loads(ClaudeHarness.auth_file(selected).content)
    assert rendered["claudeAiOauth"]["accessToken"] == "mine-token"


async def test_credential_for_a_deleted_row_raises(druks_db):
    await _seed_claude(provider_email="a@example.com")  # the surviving fallback
    gone = await _seed_claude(provider_email="b@example.com")
    gone_id = gone.id
    await gone.delete()
    # A disconnect between selection and push fails the call — it must never
    # fall through to another account's payload.
    with pytest.raises(HarnessNotConnectedError, match="removed"):
        await ProviderSubscription.lookup("anthropic", None, subscription_id=gone_id)
