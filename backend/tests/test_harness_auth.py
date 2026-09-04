import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from conftest import connect_provider
from druks.harnesses import base as hbase
from druks.harnesses.claude import ClaudeHarness, _get_credentials
from druks.harnesses.codex import CodexHarness
from druks.harnesses.datastructures import (
    SandboxSettings,
)
from druks.harnesses.exceptions import HarnessNotConnectedError
from druks.harnesses.models import ProviderLogin
from druks.harnesses.providers import AnthropicProvider, OpenAiProvider
from druks.sandbox.datastructures import HomeCopy


def _jwt(exp: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


def _claude_payload(*, access="A0", refresh="R0", expires_at=None) -> dict:
    block = {"accessToken": access, "scopes": ["user:profile"], "subscriptionType": "max"}
    if refresh is not None:
        block["refreshToken"] = refresh
    if expires_at is not None:
        block["expiresAt"] = int(expires_at.timestamp() * 1000)
    return {"claudeAiOauth": block}


async def _seed_claude(*, provider_email="op@example.com", **kwargs) -> ProviderLogin:
    return await connect_provider(
        AnthropicProvider, _claude_payload(**kwargs), provider_email=provider_email
    )


async def _seed_codex(*, provider_email="op@example.com", account_id="acc-1") -> ProviderLogin:
    access = _jwt(int((datetime.now(UTC) + timedelta(days=9)).timestamp()))
    tokens = {"access_token": access, "refresh_token": "R0", "account_id": account_id}
    return await connect_provider(
        OpenAiProvider,
        {"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": tokens},
        provider_email=provider_email,
    )


def _resp(status: int, body: object) -> httpx.Response:
    text = body if isinstance(body, str) else json.dumps(body)
    return httpx.Response(status, text=text, request=httpx.Request("GET", "https://x"))


def _mock_get(monkeypatch, response):
    calls = []

    async def fake_get(self, url, *, headers=None, **_kwargs):
        calls.append({"url": url, "headers": headers})
        return response

    monkeypatch.setattr(hbase.httpx.AsyncClient, "get", fake_get)
    return calls


def _harness() -> ClaudeHarness:
    return ClaudeHarness(model=ClaudeHarness.default_model, fast_mode=False, effort=None)


async def test_claude_builder_puts_db_credentials_on_the_bundle(druks_db):
    login = await _seed_claude(access="live", refresh="R0")
    sandbox = SandboxSettings(
        service_url="x",
        service_token="x",
        service_timeout=30.0,
        image="x",
        claude_config_dir=Path("/home/agent/.claude"),
        codex_config_dir=Path("/home/agent/.codex"),
    )
    bundle = await _get_credentials(sandbox, github_token=None, login=login)
    auth = bundle.home[0]
    assert auth.path == ".claude/.credentials.json"
    assert json.loads(auth.content)["claudeAiOauth"]["accessToken"] == "live"


async def test_credentials_builders_carry_global_instructions(druks_db):
    claude_login = await _seed_claude()
    codex_login = await _seed_codex()
    claude_config_dir = Path("/home/agent/.claude")
    codex_config_dir = Path("/home/agent/.codex")
    sandbox = SandboxSettings(
        service_url="x",
        service_token="x",
        service_timeout=30.0,
        image="x",
        claude_config_dir=claude_config_dir,
        codex_config_dir=codex_config_dir,
    )

    claude_bundle = await _get_credentials(sandbox, github_token=None, login=claude_login)
    codex_bundle = await CodexHarness(
        model=CodexHarness.default_model,
        fast_mode=False,
        effort=None,
        sandbox=sandbox,
    )._get_credentials(github_token=None, login=codex_login)

    assert claude_bundle.home[0].path == ".claude/.credentials.json"
    assert codex_bundle.home[0].path == ".codex/auth.json"
    assert HomeCopy(".claude/CLAUDE.md", claude_config_dir / "CLAUDE.md") in claude_bundle.home
    assert HomeCopy(".codex/AGENTS.md", codex_config_dir / "AGENTS.md") in codex_bundle.home


async def test_no_config_dir_ships_credential_only(druks_db):
    # No local config dir for the CLI => nothing of the host's config/plugins
    # reaches the sandbox — but the DB login still ships: the login
    # row alone decides whether a harness can run.
    login = await _seed_claude(access="live")
    sandbox = SandboxSettings(
        service_url="x",
        service_token="x",
        service_timeout=30.0,
        image="x",
        claude_config_dir=None,
        codex_config_dir=None,
    )
    bundle = await _get_credentials(sandbox, github_token="gh", login=login)
    [auth] = bundle.home
    assert json.loads(auth.content)["claudeAiOauth"]["accessToken"] == "live"
    assert bundle.github_token == "gh"


async def test_credential_without_a_selection_reads_the_fallback_account(druks_db):
    fallback = await _seed_claude(access="fallback", provider_email="a@example.com")

    assert (await ProviderLogin.lookup("anthropic", None)).id == fallback.id


async def test_credential_without_any_row_raises(druks_db):
    with pytest.raises(HarnessNotConnectedError, match="anthropic is not connected"):
        await ProviderLogin.lookup("anthropic", None)


async def test_credential_renders_only_the_selected_row(druks_db):
    mine = await _seed_claude(access="mine-token", provider_email="a@example.com")
    other = await _seed_claude(access="other-token", provider_email="b@example.com")

    selected = await ProviderLogin.lookup("anthropic", None, login_id=other.id)
    rendered = json.loads(ClaudeHarness.auth_file(selected).content)
    assert rendered["claudeAiOauth"]["accessToken"] == "other-token"
    assert "mine-token" not in json.dumps(rendered)
    selected = await ProviderLogin.lookup("anthropic", None, login_id=mine.id)
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
        await ProviderLogin.lookup("anthropic", None, login_id=gone_id)
