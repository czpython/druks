import gc
import json

import pytest
from druks.harnesses.datastructures import ParsedMetric, ParsedUsage
from druks.harnesses.providers import AnthropicProvider, Provider
from druks.usage.models import UsageScrape


@pytest.fixture(autouse=True)
def _collect_transient_providers():
    # Each test's ``_Fake(Provider)`` enters the global ``Provider.__subclasses__()``
    # registry and is held alive by a closure<->class cycle that plain refcounting
    # can't break. Force a GC pass so it's gone before another module iterates
    # the registry and trips over a fake.
    yield
    gc.collect()


def _metric(percent_left: int) -> ParsedMetric:
    return ParsedMetric(percent_left=percent_left, resets_at=None)


def _usage(
    *, ok=True, error=None, plan_tier=None, five=None, weeks=(), unlimited=False
) -> ParsedUsage:
    return ParsedUsage(
        ok=ok,
        error=error,
        plan_tier=plan_tier,
        five_hour=five,
        weeks=weeks,
        unlimited=unlimited,
        raw="{}" if ok else "",
    )


def _provider(id_: str, fetch):
    """A fake Provider: id + the fetch classmethod poll_usage calls under the
    real inherited poll. ``fetch`` is a plain callable returning ParsedUsage
    (or raising). A transient subclass, swept out of
    ``Provider.__subclasses__()`` by the autouse GC fixture above."""

    class _Fake(Provider):
        id = id_

        @classmethod
        async def fetch_usage(cls, connection, *, now=None):
            return fetch()

    return _Fake


async def _connection(email: str = "op@example.com"):
    # poll_usage reads only account_id off the connection; the account row
    # must be real (the scrape carries its FK).
    from types import SimpleNamespace

    from druks.accounts.models import Account

    return SimpleNamespace(account_id=(await Account.get_or_create(email)).id)


async def _poll(*providers) -> list[dict[str, object]]:
    # poll_usage is the unit under test: fetch -> parse -> persist a UsageScrape.
    connection = await _connection()
    return [await h.poll_usage(connection) for h in providers]


async def test_successful_fetch_persists_per_provider(druks_db) -> None:
    results = await _poll(
        _provider("anthropic", lambda: _usage(five=_metric(84), weeks=(_metric(52),))),
        _provider(
            "openai",
            lambda: _usage(plan_tier="prolite", five=_metric(61), weeks=(_metric(61),)),
        ),
    )
    await druks_db.flush()

    assert [r["status"] for r in results] == ["recorded", "recorded"]
    assert all(r["parse_ok"] for r in results)

    claude_row = await UsageScrape.latest_for("anthropic", (await _connection()).account_id)
    assert claude_row is not None
    assert claude_row.five_hour_percent_left == 84
    assert claude_row.weeks == [
        {"percent_left": 52, "resets_at": None, "model": None},
    ]

    codex_row = await UsageScrape.latest_for("openai", (await _connection()).account_id)
    assert codex_row is not None
    assert codex_row.plan_tier == "prolite"
    assert codex_row.weeks[0]["percent_left"] == 61


async def test_claude_weekly_windows_survive_parse_and_poll_in_order(druks_db) -> None:
    parsed = AnthropicProvider._parse_usage(
        json.dumps(
            {
                "five_hour": {"utilization": 20},
                "seven_day": {"utilization": 30},
                "limits": [
                    {"group": "weekly", "percent": 30, "scope": None},
                    {
                        "group": "weekly",
                        "percent": 100,
                        "scope": {"model": {"display_name": "Fable"}},
                    },
                ],
            }
        )
    )

    await _poll(_provider("anthropic", lambda: parsed))
    await druks_db.flush()

    row = await UsageScrape.latest_for("anthropic", (await _connection()).account_id)
    assert row is not None
    assert [(week["percent_left"], week["model"]) for week in row.weeks] == [
        (70, None),
        (0, "Fable"),
    ]


async def test_credential_error_records_error_snapshot(druks_db) -> None:
    results = await _poll(
        _provider("anthropic", lambda: _usage(ok=False, error="token_expired")),
        _provider("openai", lambda: _usage(ok=False, error="no_credentials")),
    )
    await druks_db.flush()
    assert all(r["status"] == "recorded" for r in results)
    assert all(not r["parse_ok"] for r in results)

    claude_row = await UsageScrape.latest_for("anthropic", (await _connection()).account_id)
    assert claude_row is not None
    assert claude_row.parse_ok is False
    assert claude_row.error == "token_expired"
    assert claude_row.five_hour_percent_left is None


async def test_fetch_crash_writes_crash_snapshot(druks_db) -> None:
    def boom() -> ParsedUsage:
        raise RuntimeError("boom")

    results = await _poll(_provider("anthropic", boom), _provider("openai", boom))
    await druks_db.flush()
    assert all(r["status"] == "errored" and r["error"] == "crashed" for r in results)

    row = await UsageScrape.latest_for("anthropic", (await _connection()).account_id)
    assert row is not None
    assert row.parse_ok is False


async def test_snapshot_persists_unlimited_flag(druks_db) -> None:
    await _poll(
        _provider(
            "openai",
            lambda: _usage(
                plan_tier="business",
                five=_metric(100),
                weeks=(_metric(100),),
                unlimited=True,
            ),
        )
    )
    await druks_db.flush()

    row = await UsageScrape.latest_for("openai", (await _connection()).account_id)
    assert row is not None
    assert row.unlimited is True


async def test_two_accounts_of_one_provider_snapshot_independently(druks_db) -> None:
    snapshots = iter([_usage(five=_metric(84)), _usage(five=_metric(30))])
    fake = _provider("anthropic", lambda: next(snapshots))
    first, second = await _connection("a@example.com"), await _connection("b@example.com")

    await fake.poll_usage(first)
    await fake.poll_usage(second)
    await druks_db.flush()

    assert (
        await UsageScrape.latest_for("anthropic", first.account_id)
    ).five_hour_percent_left == 84
    assert (
        await UsageScrape.latest_for("anthropic", second.account_id)
    ).five_hour_percent_left == 30
