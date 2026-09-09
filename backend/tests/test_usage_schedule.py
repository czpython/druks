from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import (
    connect_anthropic_subscription,
    connect_provider,
    seed_note_run,
)
from druks.core.tasks import refresh_usage
from druks.harnesses.datastructures import ParsedMetric, ParsedUsage
from druks.harnesses.providers import AnthropicProvider, OpenAiProvider
from druks.models import Base
from druks.secrets.models import VaultSecret
from druks.testing import seed_call
from druks.usage.models import UsageScrape

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)


@pytest.fixture
async def subscription(druks_db) -> VaultSecret:
    return await connect_anthropic_subscription("op@example.com")


async def _scrape(
    subscription: VaultSecret,
    at: datetime,
    *,
    five: int | None = 50,
    reset: datetime | None = None,
    weeks: list[dict[str, Any]] | None = None,
    error: str | None = None,
) -> UsageScrape:
    row = UsageScrape(
        provider=subscription.audience_name,
        account_id=subscription.account_id,
        scraped_at=at,
        five_hour_percent_left=five,
        five_hour_resets_at=reset,
        weeks=weeks or [],
        parse_ok=not error,
        error=error,
    )
    await row.save()
    return row


async def test_first_scrape_ignores_other_subscriptions(subscription) -> None:
    """A provider or account with history does not delay a new subscription."""
    other_account = await connect_anthropic_subscription("other@example.com")
    other_provider = await connect_provider(OpenAiProvider, {})
    await _scrape(other_account, NOW)
    await _scrape(other_provider, NOW)

    assert await UsageScrape.is_due(subscription, now=NOW)


@pytest.mark.parametrize("count,minutes", [(1, 5), (2, 10), (3, 20), (4, 40), (5, 60), (6, 60)])
@pytest.mark.parametrize("error", [None, "auth_required"])
async def test_unchanged_scrapes_double_interval_to_one_hour(
    subscription, count, minutes, error
) -> None:
    """Successful scrapes and unchanged errors use the same bounded interval."""
    for index in range(count):
        await _scrape(
            subscription,
            NOW - timedelta(minutes=count - index - 1),
            five=None if error else 50,
            error=error,
        )
    due_at = NOW + timedelta(minutes=minutes)

    assert not await UsageScrape.is_due(subscription, now=due_at - timedelta(seconds=1))
    assert await UsageScrape.is_due(subscription, now=due_at)


@pytest.mark.parametrize(
    "changes",
    [
        {"five": 49},
        {"five": None},
        {"error": "timeout"},
        {"weeks": [{"model": "Fable", "percent_left": 49, "resets_at": None}]},
        {"weeks": []},
    ],
)
async def test_changed_values_restart_the_interval(subscription, changes) -> None:
    """A changed quota or error starts a new five-minute interval."""
    values = {"weeks": [{"model": "Fable", "percent_left": 50, "resets_at": None}]}

    for index in range(5):
        await _scrape(subscription, NOW - timedelta(minutes=5 - index), **values)
    await _scrape(subscription, NOW, **(values | changes))

    assert not await UsageScrape.is_due(subscription, now=NOW + timedelta(minutes=4))
    assert await UsageScrape.is_due(subscription, now=NOW + timedelta(minutes=5))


@pytest.mark.parametrize("status", ["succeeded", "failed"])
async def test_finished_call_polls_on_the_next_tick(subscription, druks_db, status) -> None:
    """A call billed to this subscription bypasses the idle delay."""
    await _scrape(subscription, NOW)
    run = await seed_note_run(druks_db)
    call = await seed_call(
        druks_db, run, "summarize", subscription_id=subscription.id, status=status
    )
    call.finished_at = NOW + timedelta(seconds=1)
    await druks_db.flush()

    assert await UsageScrape.is_due(subscription, now=NOW + timedelta(minutes=1))


@pytest.mark.parametrize("source", ["other_account", "other_provider", "api_key", "running", "old"])
async def test_unrelated_or_unfinished_calls_do_not_bypass_delay(
    subscription, druks_db, source
) -> None:
    """Only a later completion on this subscription makes its scrape due."""
    await _scrape(subscription, NOW)
    charged_subscription = subscription

    if source == "other_account":
        charged_subscription = await connect_anthropic_subscription("other@example.com")
    elif source == "other_provider":
        charged_subscription = await connect_provider(OpenAiProvider, {})
    run = await seed_note_run(druks_db)
    call = await seed_call(
        druks_db,
        run,
        "summarize",
        subscription_id=None if source == "api_key" else charged_subscription.id,
    )
    call.finished_at = NOW + timedelta(seconds=1)

    if source == "running":
        call.finished_at = None
    elif source == "old":
        call.finished_at = NOW
    await druks_db.flush()

    assert not await UsageScrape.is_due(subscription, now=NOW + timedelta(minutes=1))


@pytest.mark.parametrize("window", ["five_hour", "weekly"])
async def test_finished_call_bypasses_exhausted_window(subscription, druks_db, window) -> None:
    """A completed call makes an exhausted snapshot due before its reset."""
    reset = NOW + timedelta(hours=2)
    values = {"five": 0, "reset": reset}

    if window == "weekly":
        values = {"weeks": [{"model": "Fable", "percent_left": 0, "resets_at": reset.isoformat()}]}
    await _scrape(subscription, NOW, **values)
    run = await seed_note_run(druks_db)
    call = await seed_call(druks_db, run, "summarize", subscription_id=subscription.id)
    call.finished_at = NOW + timedelta(seconds=1)
    await druks_db.flush()

    assert await UsageScrape.is_due(subscription, now=NOW + timedelta(minutes=1))


async def test_soonest_exhausted_reset_controls_polling(subscription) -> None:
    """A nonempty window's earlier reset does not end the exhaustion delay."""
    await _scrape(
        subscription,
        NOW,
        five=0,
        reset=NOW + timedelta(hours=2),
        weeks=[
            {
                "model": None,
                "percent_left": 50,
                "resets_at": (NOW + timedelta(minutes=5)).isoformat(),
            },
            {
                "model": "Fable",
                "percent_left": 0,
                "resets_at": (NOW + timedelta(hours=1)).isoformat(),
            },
        ],
    )

    assert not await UsageScrape.is_due(subscription, now=NOW + timedelta(minutes=59))
    assert await UsageScrape.is_due(subscription, now=NOW + timedelta(hours=1))


@pytest.mark.parametrize("reset", [None, NOW - timedelta(seconds=1), NOW])
async def test_zero_without_a_future_reset_uses_normal_interval(subscription, reset) -> None:
    """An unknown or expired reset cannot block polling indefinitely."""
    await _scrape(subscription, NOW, five=0, reset=reset)

    assert not await UsageScrape.is_due(subscription, now=NOW + timedelta(minutes=4))
    assert await UsageScrape.is_due(subscription, now=NOW + timedelta(minutes=5))


@pytest.mark.parametrize("error", [None, "timeout"])
async def test_eight_idle_hours_need_at_most_twelve_polls(subscription, error) -> None:
    """Five-minute ticks respect the hourly cap throughout an idle stretch."""
    polls = 0

    for minute in range(0, 8 * 60 + 1, 5):
        now = NOW + timedelta(minutes=minute)

        if await UsageScrape.is_due(subscription, now=now):
            await _scrape(subscription, now, five=None if error else 50, error=error)
            polls += 1

    assert 8 <= polls <= 12


async def test_task_polls_only_due_subscriptions(subscription, monkeypatch) -> None:
    """The task polls only due subscriptions across providers."""
    idle = await connect_anthropic_subscription("idle@example.com")
    exhausted = await connect_anthropic_subscription("exhausted@example.com")
    revoked = await connect_anthropic_subscription("revoked@example.com")
    revoked.revoked_at = NOW
    openai = await connect_provider(OpenAiProvider, {})
    await _scrape(idle, NOW - timedelta(minutes=1))
    await _scrape(exhausted, NOW, five=0, reset=NOW + timedelta(hours=1))
    expected = [subscription.id, openai.id]
    fetched = []

    async def fetch_usage(subscription, *, now=None):
        fetched.append(subscription.id)
        return ParsedUsage(ok=True, five_hour=ParsedMetric(percent_left=50, resets_at=None))

    monkeypatch.setattr(Base, "utc_now", lambda: NOW)
    monkeypatch.setattr(AnthropicProvider, "fetch_usage", fetch_usage)
    monkeypatch.setattr(OpenAiProvider, "fetch_usage", fetch_usage)

    await refresh_usage._function()

    assert fetched == expected
    assert (await UsageScrape.latest_for("openai", openai.account_id)).scraped_at == NOW
