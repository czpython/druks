import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import connect_provider
from druks.accounts.models import Account
from druks.harnesses import providers as pbase
from druks.harnesses.constants import DIRECTORY_CACHE_KEY
from druks.harnesses.directory import (
    add_provider,
    list_providers,
    parse_providers,
    refresh_added_catalogs,
)
from druks.harnesses.exceptions import CatalogError
from druks.harnesses.models import ProviderCatalog, ProviderKey
from druks.harnesses.providers import AnthropicProvider, OpenAiProvider
from druks.redis import get_client

_LLAMA = {"id": "groq/llama-4", "label": "Llama 4"}
_GROQ = {"name": "Groq", "env": ["GROQ_API_KEY"], "models": {"llama-4": {"name": "Llama 4"}}}


def _resp(status: int, body: object) -> httpx.Response:
    text = body if isinstance(body, str) else json.dumps(body)
    return httpx.Response(status, text=text, request=httpx.Request("GET", "https://x"))


def _mock_get(monkeypatch, response):
    calls = []

    async def fake_get(self, url, *, headers=None, **_kwargs):
        calls.append({"url": url, "headers": headers})
        return response

    monkeypatch.setattr(pbase.httpx.AsyncClient, "get", fake_get)
    return calls


def _claude_login(**kwargs):
    block = {
        "accessToken": "tok",
        "expiresAt": int((datetime.now(UTC) + timedelta(hours=2)).timestamp() * 1000),
    }
    return connect_provider(AnthropicProvider, {"claudeAiOauth": block}, **kwargs)


def test_anthropic_parse_namespaces_ids_and_keeps_labels() -> None:
    payload = json.dumps(
        {
            "data": [
                {"id": "claude-fable-5", "display_name": "Claude Fable 5", "type": "model"},
                {"id": "claude-opus-4-8"},
            ],
            "has_more": False,
        }
    )

    assert AnthropicProvider._parse_catalog(payload, billing="subscription") == (
        {"id": "anthropic/claude-fable-5", "label": "Claude Fable 5"},
        {"id": "anthropic/claude-opus-4-8", "label": "claude-opus-4-8"},
    )


@pytest.mark.parametrize(
    ("body", "tag"),
    [
        ('{"data": []}', "empty_list"),
        ('{"models": []}', "unexpected_payload"),
        ("<!doctype html>", "unparseable"),
    ],
)
def test_anthropic_parse_names_why_a_body_offers_nothing(body, tag) -> None:
    with pytest.raises(CatalogError) as error:
        AnthropicProvider._parse_catalog(body, billing="subscription")
    assert error.value.tag == tag


def test_codex_parse_keeps_only_listed_models() -> None:
    payload = json.dumps(
        {
            "models": [
                {
                    "slug": "gpt-5.6-sol",
                    "display_name": "GPT-5.6-Sol",
                    "visibility": "list",
                    "minimal_client_version": "0.144.0",
                    "supported_reasoning_levels": [{"effort": "low"}, {"effort": "xhigh"}],
                },
                {
                    "slug": "codex-auto-review",
                    "display_name": "Codex Auto Review",
                    "visibility": "hide",
                },
            ]
        }
    )

    assert OpenAiProvider._parse_catalog(payload, billing="subscription") == (
        {
            "id": "openai/gpt-5.6-sol",
            "label": "GPT-5.6-Sol",
            "efforts": ["low", "xhigh"],
            "minimal_client_version": "0.144.0",
        },
    )


def test_codex_parse_empty_catalog_is_an_error() -> None:
    """A stale-low ``client_version`` yields ``200 {"models": []}`` — that must
    never read as "no models" and wipe the stored list."""
    with pytest.raises(CatalogError, match="empty_list"):
        OpenAiProvider._parse_catalog(json.dumps({"models": []}), billing="subscription")


def test_openai_parse_keeps_the_chat_models_of_its_own_list() -> None:
    raw = json.dumps(
        {
            "data": [
                {"id": "gpt-5.5", "object": "model"},
                {"id": "o3-mini", "object": "model"},
                {"id": "gpt-4o-realtime-preview", "object": "model"},
                {"id": "whisper-1", "object": "model"},
                {"id": "text-embedding-3-small", "object": "model"},
            ]
        }
    )

    assert OpenAiProvider._parse_catalog(raw, billing="api_key") == (
        {"id": "openai/gpt-5.5", "label": "gpt-5.5"},
        {"id": "openai/o3-mini", "label": "o3-mini"},
    )
    with pytest.raises(CatalogError, match="unexpected_payload"):
        OpenAiProvider._parse_catalog(json.dumps({"models": []}), billing="api_key")
    with pytest.raises(CatalogError, match="empty_list"):
        OpenAiProvider._parse_catalog(
            json.dumps({"data": [{"id": "whisper-1"}]}), billing="api_key"
        )


def test_directory_keeps_one_key_providers_with_exact_ids() -> None:
    providers = parse_providers(
        json.dumps(
            {
                "openrouter": {
                    "id": "different-nested-id",
                    "name": "OpenRouter",
                    "env": ["OPENROUTER_API_KEY"],
                    "models": {
                        "anthropic/claude-sonnet-4": {
                            "id": "different-model-id",
                            "name": "Claude Sonnet 4",
                        }
                    },
                },
                "amazon-bedrock": {
                    "name": "Amazon Bedrock",
                    "env": ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"],
                    "models": {"nova": {"name": "Nova"}},
                },
            }
        )
    )

    assert providers == [
        {
            "provider": "openrouter",
            "label": "OpenRouter",
            "models": [{"id": "openrouter/anthropic/claude-sonnet-4", "label": "Claude Sonnet 4"}],
        }
    ]
    with pytest.raises(CatalogError, match="unparseable"):
        parse_providers("<!doctype html>")
    with pytest.raises(CatalogError, match="empty_list"):
        parse_providers(json.dumps({"amazon-bedrock": {"env": ["A", "B"], "models": {}}}))


async def test_anthropic_fetch_uses_the_oauth_token(monkeypatch, druks_db):
    subscription = await _claude_login()
    body = {"data": [{"id": "claude-fable-5", "display_name": "Claude Fable 5"}]}
    calls = _mock_get(monkeypatch, _resp(200, body))

    models = await AnthropicProvider.fetch_catalog(subscription)

    assert models == ({"id": "anthropic/claude-fable-5", "label": "Claude Fable 5"},)
    assert calls[0]["url"] == "https://api.anthropic.com/v1/models?limit=100"
    assert calls[0]["headers"]["Authorization"] == "Bearer tok"


async def test_anthropic_fetch_uses_the_api_key(monkeypatch):
    calls = _mock_get(monkeypatch, _resp(200, {"data": [{"id": "claude-fable-5"}]}))

    await AnthropicProvider.fetch_catalog(key="sk-ant")

    assert calls[0]["headers"]["x-api-key"] == "sk-ant"
    assert "Authorization" not in calls[0]["headers"]


async def test_codex_fetch_sends_the_account_header(monkeypatch, druks_db):
    tokens = {"access_token": "a.b.c", "account_id": "acc-7"}
    subscription = await connect_provider(OpenAiProvider, {"tokens": tokens})
    calls = _mock_get(monkeypatch, _resp(200, {"models": []}))

    with pytest.raises(CatalogError, match="empty_list"):
        await OpenAiProvider.fetch_catalog(subscription)
    assert calls[0]["url"] == "https://chatgpt.com/backend-api/codex/models?client_version=99.99.99"
    assert calls[0]["headers"]["ChatGPT-Account-Id"] == "acc-7"


async def test_fetch_names_an_http_failure(monkeypatch, druks_db):
    subscription = await _claude_login()
    _mock_get(monkeypatch, _resp(503, "down"))

    with pytest.raises(CatalogError, match="http_503"):
        await AnthropicProvider.fetch_catalog(subscription)


async def test_refresh_stores_the_catalog_and_keeps_it_on_failure(monkeypatch, druks_db):
    await _claude_login()
    _mock_get(monkeypatch, _resp(200, {"data": [{"id": "claude-fable-5"}]}))
    await AnthropicProvider.refresh_catalog()
    [stored] = await ProviderCatalog.list_all()
    assert stored.provider == "anthropic"
    assert stored.models == [{"id": "anthropic/claude-fable-5", "label": "claude-fable-5"}]

    _mock_get(monkeypatch, _resp(200, {"data": []}))
    await AnthropicProvider.refresh_catalog()
    [kept] = await ProviderCatalog.list_all()
    assert kept.models == stored.models


async def test_refresh_without_a_login_stores_nothing(monkeypatch, druks_db):
    calls = _mock_get(monkeypatch, _resp(200, {"data": [{"id": "claude-fable-5"}]}))
    await AnthropicProvider.refresh_catalog()
    assert calls == []
    assert await ProviderCatalog.list_all() == []


async def test_openai_refresh_reads_its_own_list_over_the_key(monkeypatch, druks_db):
    account = await Account.get_or_create("op@example.com")
    await ProviderKey.create(provider="openai", key="sk-openai", account=account)
    calls = _mock_get(monkeypatch, _resp(200, {"data": [{"id": "gpt-5.5"}, {"id": "whisper-1"}]}))
    await OpenAiProvider.refresh_catalog()
    assert calls[0]["url"] == "https://api.openai.com/v1/models"
    assert calls[0]["headers"] == {"Authorization": "Bearer sk-openai"}
    [stored] = await ProviderCatalog.list_all()
    assert stored.label == "OpenAI"
    assert stored.models == [{"id": "openai/gpt-5.5", "label": "gpt-5.5"}]

    # A subscription's endpoint wins once one exists.
    await connect_provider(
        OpenAiProvider,
        {"tokens": {"access_token": "a.b.c", "account_id": "acc-7"}},
        provider_email="seat@example.com",
    )
    calls = _mock_get(
        monkeypatch,
        _resp(200, {"models": [{"slug": "gpt-5.6", "visibility": "list"}]}),
    )
    await OpenAiProvider.refresh_catalog()
    assert calls[0]["url"].startswith("https://chatgpt.com/backend-api/codex/models")
    [stored] = await ProviderCatalog.list_all()
    assert [model["id"] for model in stored.models] == ["openai/gpt-5.6"]


async def test_directory_is_fetched_once_and_read_from_redis(monkeypatch, druks_redis):
    calls = _mock_get(monkeypatch, _resp(200, {"groq": _GROQ}))

    first = await list_providers()
    second = await list_providers()

    assert first == second == [{"provider": "groq", "label": "Groq", "models": [_LLAMA]}]
    assert [call["url"] for call in calls] == ["https://models.dev/api.json"]

    _mock_get(monkeypatch, _resp(503, "down"))
    await get_client().delete(DIRECTORY_CACHE_KEY)
    with pytest.raises(CatalogError, match="http_503"):
        await list_providers()


async def test_adding_a_directory_provider_creates_its_catalog(monkeypatch, druks_db, druks_redis):
    _mock_get(monkeypatch, _resp(200, {"groq": _GROQ}))

    added = await add_provider("groq")

    assert (added.provider, added.label, added.models) == ("groq", "Groq", [_LLAMA])
    with pytest.raises(KeyError):
        await add_provider("nobody")


async def test_added_catalogs_refresh_from_the_directory(monkeypatch, druks_db, druks_redis):
    account = await Account.get_or_create("op@example.com")
    await ProviderKey.create(provider="groq", key="gsk", account=account)
    await ProviderKey.create(provider="anthropic", key="sk-ant", account=account)
    calls = _mock_get(monkeypatch, _resp(200, {"groq": _GROQ}))

    await refresh_added_catalogs()

    # Only the added provider reads the directory; anthropic is registered.
    assert [call["url"] for call in calls] == ["https://models.dev/api.json"]
    [stored] = await ProviderCatalog.list_all()
    assert (stored.provider, stored.label) == ("groq", "Groq")
