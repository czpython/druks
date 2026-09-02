from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from conftest import connect_provider
from druks.accounts.models import Account
from druks.harnesses.datastructures import ParsedMetric, ParsedUsage
from druks.harnesses.models import ProviderLogin
from druks.harnesses.providers import AnthropicProvider
from druks.settings import Settings
from druks.testing import configure_app_for_test, make_settings, seed_call, seed_run
from druks.usage.models import UsageScrape
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize
from fastapi.testclient import TestClient


@pytest.fixture
def app_settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def client(app_settings: Settings):
    with TestClient(configure_app_for_test(settings=app_settings)) as c:
        yield c


async def _account_id() -> str:
    # The suite's auth gate stands in op@example.com (conftest override).
    return (await Account.get_or_create("op@example.com")).id


async def _seed(snapshots: list[UsageScrape]) -> None:
    # save() flushes onto the ambient per-test connection session (bound by the
    # _txn fixture), so the rows are visible to the request and roll back with
    # the test — no separate engine, no commit. Every snapshot belongs to the
    # viewing account unless a test stamps another owner.
    viewer = await _account_id()
    for snap in snapshots:
        if not snap.account_id:
            snap.account_id = viewer
        await snap.save()


def _provider(body: dict, provider_id: str) -> dict:
    return next(entry for entry in body["providers"] if entry["id"] == provider_id)


async def _seed_agent_call(druks_db, *, model: str = "gpt-5.5"):
    note = await Note.create(body="usage accounting")
    run = await seed_run(druks_db, kind=Summarize.kind, subject=note)
    return await seed_call(druks_db, run, "summarize", status="running", model=model)


async def test_usage_today_counts_calls_whose_model_no_picker_claims(client, druks_db) -> None:
    # Model ids churn on deploys (opus-4-7 → 4-8), so a call finished earlier today
    # can carry an id no picker claims any more. Money spent must not vanish from
    # the display — the sys-strip's total_run_spend_between counts every call, and
    # the two surfaces must quote the same number. Unclaimed models land in the
    # "unattributed" bucket the panel's grand total sums.
    call = await _seed_agent_call(druks_db, model="claude-opus-4-5")
    call.account_id = await _account_id()
    call.finished_at = datetime.now(UTC)
    call.cost_usd = 2.5
    await druks_db.flush()

    body = client.get("/api/usage/today").json()
    bucket = _provider(body, "unattributed")
    assert bucket["runs"] == 1
    assert bucket["spendUsd"] == 2.5


def test_get_usage_empty_returns_available_false(client) -> None:
    response = client.get("/api/usage")
    assert response.status_code == 200
    body = response.json()
    # One entry per registered provider, none available pre-first-poll.
    assert {entry["id"] for entry in body["providers"]} == {"anthropic", "openai", "openai-codex"}
    assert all(entry["available"] is False for entry in body["providers"])


async def test_get_usage_presents_api_key_connection_as_unmetered(client, druks_db) -> None:
    await ProviderLogin.connect(
        provider="openai",
        account=await Account.get_or_create("op@example.com"),
        payload={"api_key": "key"},
        expires_at=None,
        provider_email="op@example.com",
        kind="api_key",
    )

    summary = _provider(client.get("/api/usage").json(), "openai")

    assert summary["available"] is True
    assert summary["connected"] is True
    assert summary["unlimited"] is True
    assert summary["fiveHour"] is None
    assert summary["weeks"] == []


async def test_get_usage_serializes_latest_per_provider(client, app_settings) -> None:
    # Plant a snapshot for anthropic only — openai-codex should still report
    # ``available=false`` rather than missing-key/404.
    await _seed(
        [
            UsageScrape(
                provider="anthropic",
                parse_ok=True,
                plan_tier="Max",
                five_hour_percent_left=54,
                five_hour_resets_at=datetime(2026, 5, 23, 18, 40, tzinfo=UTC),
                weeks=[
                    {"percent_left": 38, "resets_at": None, "model": None},
                    {"percent_left": 0, "resets_at": None, "model": "Fable"},
                ],
                scraped_at=datetime.now(UTC) - timedelta(seconds=45),
            ),
        ],
    )

    response = client.get("/api/usage")
    assert response.status_code == 200
    body = response.json()

    claude = _provider(body, "anthropic")
    assert claude["available"] is True
    assert claude["planTier"] == "Max"
    assert claude["fiveHour"]["percentLeft"] == 54
    assert [(week["percentLeft"], week["model"]) for week in claude["weeks"]] == [
        (38, None),
        (0, "Fable"),
    ]
    assert claude["ageSeconds"] is not None
    assert 30 <= claude["ageSeconds"] <= 90  # close to the planted 45s
    assert claude["stale"] is False

    assert _provider(body, "openai-codex")["available"] is False


async def test_get_usage_flags_stale_after_24h(client, app_settings) -> None:
    await _seed(
        [
            UsageScrape(
                provider="anthropic",
                parse_ok=True,
                five_hour_percent_left=10,
                scraped_at=datetime.now(UTC) - timedelta(hours=30),
            ),
        ],
    )

    body = client.get("/api/usage").json()
    assert _provider(body, "anthropic")["stale"] is True


async def test_get_usage_exposes_unlimited_flag(client, app_settings) -> None:
    # ChatGPT business plan: scraper synthesizes permanently-full buckets
    # and marks the row unmetered so the UI can render "unmetered"
    # instead of a quota bar that never moves.
    await _seed(
        [
            UsageScrape(
                provider="openai-codex",
                parse_ok=True,
                plan_tier="business",
                five_hour_percent_left=100,
                weeks=[{"percent_left": 100, "resets_at": None, "model": None}],
                unlimited=True,
            ),
        ],
    )

    body = client.get("/api/usage").json()
    assert _provider(body, "openai-codex")["unlimited"] is True
    assert _provider(body, "openai-codex")["fiveHour"]["percentLeft"] == 100
    assert _provider(body, "anthropic")["unlimited"] is False


async def test_usage_history_serializes_series_oldest_first(client, app_settings) -> None:
    now = datetime.now(UTC)
    snaps = [
        UsageScrape(
            provider="anthropic",
            parse_ok=True,
            five_hour_percent_left=pct,
            weeks=[
                {"percent_left": 90 - i, "resets_at": None, "model": None},
                {"percent_left": 40 - i, "resets_at": None, "model": "Fable"},
            ],
            scraped_at=now - timedelta(minutes=10 * i),
        )
        for i, pct in enumerate([20, 40, 60])
    ]
    # Outside the 6h five-hour range but inside the weekly range.
    snaps.append(
        UsageScrape(
            provider="anthropic",
            parse_ok=True,
            five_hour_percent_left=95,
            weeks=[
                {"percent_left": 99, "resets_at": None, "model": None},
                {"percent_left": 49, "resets_at": None, "model": "Fable"},
            ],
            scraped_at=now - timedelta(hours=12),
        ),
    )
    # Failed scrape — no percentages, must not appear in either series.
    snaps.append(
        UsageScrape(provider="anthropic", parse_ok=False, scraped_at=now - timedelta(minutes=5))
    )
    await _seed(snaps)

    body = client.get("/api/usage/history").json()

    assert [p["pct"] for p in _provider(body, "anthropic")["fiveHour"]] == [60, 40, 20]
    assert [series["model"] for series in _provider(body, "anthropic")["weeks"]] == [None, "Fable"]
    assert [p["pct"] for p in _provider(body, "anthropic")["weeks"][0]["points"]] == [
        99,
        88,
        89,
        90,
    ]
    assert [p["pct"] for p in _provider(body, "anthropic")["weeks"][1]["points"]] == [
        49,
        38,
        39,
        40,
    ]
    assert _provider(body, "openai-codex")["fiveHour"] == []
    assert _provider(body, "openai-codex")["weeks"] == []


async def test_usage_today_aggregates_spend_and_tokens_by_provider(
    client, app_settings, druks_db
) -> None:
    codex_run = await _seed_agent_call(druks_db, model="gpt-5.5")
    codex_run.account_id = await _account_id()
    codex_run.cost_usd = 1.25
    codex_run.cost_metadata = {
        "provider": "openai",
        "input_tokens": 1000,
        "output_tokens": 200,
        "reasoning_output_tokens": 50,
    }
    codex_run.finished_at = datetime.now(UTC)

    claude_run = await _seed_agent_call(druks_db, model="claude-opus-4-7")
    claude_run.account_id = await _account_id()
    claude_run.cost_usd = 2.5
    claude_run.cost_metadata = {
        "provider": "anthropic",
        "input_tokens": 100,
        "cache_read_input_tokens": 50,
        "cache_creation_input_tokens": 25,
        "output_tokens": 75,
    }
    claude_run.finished_at = datetime.now(UTC)

    # Finished yesterday — outside today's boundary, must not count.
    old_run = await _seed_agent_call(druks_db, model="gpt-5.5")
    old_run.cost_usd = 99.0
    old_run.finished_at = datetime.now(UTC) - timedelta(days=2)

    # Still running — no cost yet, counted nowhere.
    await _seed_agent_call(druks_db, model="gpt-5.5")
    await druks_db.flush()

    body = client.get("/api/usage/today").json()

    codex = _provider(body, "openai-codex")
    assert codex["spendUsd"] == 1.25
    assert codex["tokens"] == 1250  # 1000 input + (200 + 50) output
    assert codex["runs"] == 1
    claude = _provider(body, "anthropic")
    assert claude["spendUsd"] == 2.5
    assert claude["tokens"] == 250  # (100 + 50 + 25) input + 75 output
    assert claude["runs"] == 1

    hour = datetime.now(ZoneInfo(body["timezone"])).hour
    assert codex["hours"][hour] == 1.25
    assert claude["hours"][hour] == 2.5
    assert sum(codex["hours"]) == 1.25
    assert sum(claude["hours"]) == 2.5


async def test_usage_excludes_another_accounts_scrape(client, druks_db) -> None:
    snap = UsageScrape(provider="anthropic", parse_ok=True, five_hour_percent_left=54)
    snap.account_id = (await Account.get_or_create("other@example.com")).id
    await snap.save()

    body = client.get("/api/usage").json()
    assert _provider(body, "anthropic")["available"] is False
    history = client.get("/api/usage/history").json()
    assert _provider(history, "anthropic")["fiveHour"] == []


async def test_usage_reports_viewers_login_identity(client, druks_db) -> None:
    await ProviderLogin.connect(
        provider="anthropic",
        account=await Account.get_or_create("other@example.com"),
        payload={"claudeAiOauth": {"accessToken": "other"}},
        expires_at=None,
        provider_email="other-seat@example.com",
        kind="oauth",
    )
    body = client.get("/api/usage").json()
    assert _provider(body, "anthropic")["connected"] is False
    assert _provider(body, "anthropic")["providerEmail"] is None

    await ProviderLogin.connect(
        provider="anthropic",
        account=await Account.get_or_create("op@example.com"),
        payload={"claudeAiOauth": {"accessToken": "mine"}},
        expires_at=None,
        provider_email="subscription@example.com",
        kind="oauth",
    )
    body = client.get("/api/usage").json()
    assert _provider(body, "anthropic")["connected"] is True
    assert _provider(body, "anthropic")["providerEmail"] == "subscription@example.com"


async def test_usage_today_counts_only_the_viewers_calls(client, druks_db) -> None:
    mine = await _seed_agent_call(druks_db, model="claude-opus-4-7")
    mine.account_id = await _account_id()
    mine.cost_usd = 2.0
    mine.finished_at = datetime.now(UTC)

    other = await _seed_agent_call(druks_db, model="claude-opus-4-7")
    other.account_id = (await Account.get_or_create("other@example.com")).id
    other.cost_usd = 5.0
    other.finished_at = datetime.now(UTC)

    background = await _seed_agent_call(druks_db, model="claude-opus-4-7")
    background.cost_usd = 9.0
    background.finished_at = datetime.now(UTC)
    await druks_db.flush()

    body = client.get("/api/usage/today").json()
    assert _provider(body, "anthropic")["spendUsd"] == 2.0
    assert _provider(body, "anthropic")["runs"] == 1


def _fake_fetch(fetched: list):
    async def fake(connection, *, now=None):
        fetched.append(connection.account_id)
        return ParsedUsage(
            ok=True,
            error=None,
            plan_tier=None,
            five_hour=ParsedMetric(percent_left=50, resets_at=None),
            weeks=(),
            unlimited=False,
            raw="{}",
        )

    return fake


async def test_refresh_scrapes_only_the_viewers_logins(client, druks_db, monkeypatch) -> None:
    viewer = await connect_provider(AnthropicProvider, {"claudeAiOauth": {"accessToken": "t"}})
    await connect_provider(
        AnthropicProvider,
        {"claudeAiOauth": {"accessToken": "t2"}},
        provider_email="other@example.com",
    )
    fetched: list[str] = []
    monkeypatch.setattr(AnthropicProvider, "fetch_usage", _fake_fetch(fetched))

    assert client.post("/api/usage/refresh").status_code == 200
    assert fetched == [viewer.account_id]
    assert (
        await UsageScrape.latest_for("anthropic", viewer.account_id)
    ).five_hour_percent_left == 50


async def test_refresh_skips_a_non_metered_login(client, druks_db, monkeypatch) -> None:
    account = await Account.get_or_create("op@example.com")
    await ProviderLogin.connect(
        provider="anthropic",
        account=account,
        payload={"api_key": "key"},
        expires_at=None,
        provider_email="op@example.com",
        kind="api_key",
    )
    poll_usage = AsyncMock()
    monkeypatch.setattr(AnthropicProvider, "poll_usage", poll_usage)

    assert client.post("/api/usage/refresh").status_code == 200
    poll_usage.assert_not_awaited()


async def test_refresh_floors_repeat_scrapes(client, druks_db, monkeypatch) -> None:
    await connect_provider(AnthropicProvider, {"claudeAiOauth": {"accessToken": "t"}})
    fetched: list[str] = []
    monkeypatch.setattr(AnthropicProvider, "fetch_usage", _fake_fetch(fetched))

    client.post("/api/usage/refresh")
    client.post("/api/usage/refresh")
    assert len(fetched) == 1
