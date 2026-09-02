import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from conftest import connect_provider
from druks.harnesses import base as hbase
from druks.harnesses.claude import ClaudeHarness, _get_credentials
from druks.harnesses.codex import CodexHarness
from druks.harnesses.datastructures import (
    AgentInvocation,
    HarnessRunResult,
    ParsedModels,
    SandboxSettings,
)
from druks.harnesses.exceptions import HarnessNotConnectedError
from druks.harnesses.models import ProviderLogin
from druks.harnesses.providers import AnthropicProvider, OpenAiCodexProvider
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
        OpenAiCodexProvider,
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


async def test_claude_fetch_models_success(monkeypatch, druks_db):
    # fetch_models reads the wall clock (no ``now=`` seam), so the token's
    # expiry must be real-future, not _NOW-relative.
    login = await _seed_claude(access="tok", expires_at=datetime.now(UTC) + timedelta(hours=2))
    body = {"data": [{"id": "claude-fable-5", "display_name": "Claude Fable 5"}]}
    calls = _mock_get(monkeypatch, _resp(200, body))

    parsed = await ClaudeHarness.fetch_models(login)

    assert parsed == ParsedModels(
        ok=True,
        models=({"id": "claude-fable-5", "label": "Claude Fable 5"},),
        raw=json.dumps(body),
    )
    assert calls[0]["url"] == "https://api.anthropic.com/v1/models?limit=100"
    assert calls[0]["headers"]["Authorization"] == "Bearer tok"


async def test_codex_fetch_models_success(monkeypatch, druks_db):
    login = await _seed_codex(account_id="acc-7")
    body = {
        "models": [
            {
                "slug": "gpt-5.6-sol",
                "display_name": "GPT-5.6-Sol",
                "visibility": "list",
                "supported_reasoning_levels": [{"effort": "high"}],
                "minimal_client_version": "0.144.0",
            }
        ]
    }
    calls = _mock_get(monkeypatch, _resp(200, body))

    parsed = await CodexHarness.fetch_models(login)

    assert parsed == ParsedModels(
        ok=True,
        models=(
            {
                "id": "gpt-5.6-sol",
                "label": "GPT-5.6-Sol",
                "efforts": ["high"],
                "minimal_client_version": "0.144.0",
            },
        ),
        raw=json.dumps(body),
    )
    assert calls[0]["url"] == (
        "https://chatgpt.com/backend-api/codex/models?client_version=99.99.99"
    )
    assert calls[0]["headers"]["ChatGPT-Account-Id"] == "acc-7"


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

    assert (await _harness().login(None)).id == fallback.id


async def test_credential_without_any_row_raises(druks_db):
    with pytest.raises(HarnessNotConnectedError, match="anthropic is not connected"):
        await _harness().login(None)


async def test_credential_renders_only_the_selected_row(druks_db):
    mine = await _seed_claude(access="mine-token", provider_email="a@example.com")
    other = await _seed_claude(access="other-token", provider_email="b@example.com")

    rendered = json.loads(ClaudeHarness.auth_file(await _harness().login(other.id)).content)
    assert rendered["claudeAiOauth"]["accessToken"] == "other-token"
    assert "mine-token" not in json.dumps(rendered)
    rendered = json.loads(ClaudeHarness.auth_file(await _harness().login(mine.id)).content)
    assert rendered["claudeAiOauth"]["accessToken"] == "mine-token"


async def test_credential_for_a_deleted_row_raises(druks_db):
    await _seed_claude(provider_email="a@example.com")  # the surviving fallback
    gone = await _seed_claude(provider_email="b@example.com")
    gone_id = gone.id
    await gone.delete()
    # A disconnect between selection and push fails the call — it must never
    # fall through to another account's payload.
    with pytest.raises(HarnessNotConnectedError, match="removed"):
        await _harness().login(gone_id)


async def test_minimal_harness_reports_unsupported_model_discovery(monkeypatch):
    class MinimalHarness(hbase.Harness):
        name = "minimal"
        provider = "anthropic"
        login_kinds = frozenset({"oauth"})

        async def build_invocation(self, **kwargs: object) -> AgentInvocation:
            raise AssertionError("not called")

        def parse(
            self,
            result: HarnessRunResult,
            *,
            artifact_dir: Path,
            run_id: str,
        ) -> object:
            return {}

    login = SimpleNamespace(provider="anthropic", payload={"claudeAiOauth": {"accessToken": "t"}})
    calls = _mock_get(monkeypatch, _resp(200, {}))

    assert await MinimalHarness.fetch_models(login) == ParsedModels(ok=False, error="unsupported")
    assert calls == []
