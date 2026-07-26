import gc

import pytest
from druks.harnesses.base import Harness
from druks.harnesses.datastructures import ParsedMetric, ParsedUsage
from druks.usage.models import UsageScrape


@pytest.fixture(autouse=True)
def _collect_transient_harnesses():
    # Each test's ``_Fake(Harness)`` enters the global ``Harness.__subclasses__()``
    # registry and is held alive by a closure<->class cycle that plain refcounting
    # can't break. Force a GC pass so it's gone before another module's
    # ``seed_harnesses`` iterates the registry and trips over a fake with no
    # ``default_model``.
    yield
    gc.collect()


def _metric(percent_left: int) -> ParsedMetric:
    return ParsedMetric(percent_left=percent_left, resets_at=None)


def _usage(
    *, ok=True, error=None, plan_tier=None, five=None, week=None, unlimited=False
) -> ParsedUsage:
    return ParsedUsage(
        ok=ok,
        error=error,
        plan_tier=plan_tier,
        five_hour=five,
        week=week,
        unlimited=unlimited,
        raw="{}" if ok else "",
    )


def _harness(name_: str, fetch):
    """A fake Harness: name + the two classmethods poll_usage touches under
    the real inherited poll. ``fetch`` is a plain callable returning
    ParsedUsage (or raising). A transient subclass, swept out of
    ``Harness.__subclasses__()`` by the autouse GC fixture above."""

    class _Fake(Harness):
        name = name_

        @classmethod
        async def fetch_usage(cls, connection, *, now=None):
            return fetch()

    return _Fake


def _connection(email: str = "op@example.com"):
    # poll_usage reads only account_id off the connection; the account row
    # must be real (the scrape carries its FK).
    from types import SimpleNamespace

    from druks.accounts.models import Account

    return SimpleNamespace(account_id=Account.get_or_create(email).id)


async def _poll(*harnesses) -> list[dict[str, object]]:
    # poll_usage is the unit under test: fetch -> parse -> persist a UsageScrape.
    connection = _connection()
    return [await h.poll_usage(connection) for h in harnesses]


async def test_successful_fetch_persists_per_harness(druks_db) -> None:
    results = await _poll(
        _harness("claude", lambda: _usage(five=_metric(84), week=_metric(52))),
        _harness(
            "codex",
            lambda: _usage(plan_tier="prolite", five=_metric(61), week=_metric(61)),
        ),
    )
    druks_db.flush()

    assert [r["status"] for r in results] == ["recorded", "recorded"]
    assert all(r["parse_ok"] for r in results)

    claude_row = UsageScrape.latest_for("claude", _connection().account_id)
    assert claude_row is not None
    assert claude_row.five_hour_percent_left == 84
    assert claude_row.week_percent_left == 52

    codex_row = UsageScrape.latest_for("codex", _connection().account_id)
    assert codex_row is not None
    assert codex_row.plan_tier == "prolite"
    assert codex_row.week_percent_left == 61


async def test_credential_error_records_error_snapshot(druks_db) -> None:
    results = await _poll(
        _harness("claude", lambda: _usage(ok=False, error="token_expired")),
        _harness("codex", lambda: _usage(ok=False, error="no_credentials")),
    )
    druks_db.flush()
    assert all(r["status"] == "recorded" for r in results)
    assert all(not r["parse_ok"] for r in results)

    claude_row = UsageScrape.latest_for("claude", _connection().account_id)
    assert claude_row is not None
    assert claude_row.parse_ok is False
    assert claude_row.error == "token_expired"
    assert claude_row.five_hour_percent_left is None


async def test_fetch_crash_writes_crash_snapshot(druks_db) -> None:
    def boom() -> ParsedUsage:
        raise RuntimeError("boom")

    results = await _poll(_harness("claude", boom), _harness("codex", boom))
    druks_db.flush()
    assert all(r["status"] == "errored" and r["error"] == "crashed" for r in results)

    row = UsageScrape.latest_for("claude", _connection().account_id)
    assert row is not None
    assert row.parse_ok is False


async def test_snapshot_persists_unlimited_flag(druks_db) -> None:
    await _poll(
        _harness(
            "codex",
            lambda: _usage(
                plan_tier="business", five=_metric(100), week=_metric(100), unlimited=True
            ),
        )
    )
    druks_db.flush()

    row = UsageScrape.latest_for("codex", _connection().account_id)
    assert row is not None
    assert row.unlimited is True


async def test_two_accounts_of_one_harness_snapshot_independently(druks_db) -> None:
    snapshots = iter([_usage(five=_metric(84)), _usage(five=_metric(30))])
    fake = _harness("claude", lambda: next(snapshots))
    first, second = _connection("a@example.com"), _connection("b@example.com")

    await fake.poll_usage(first)
    await fake.poll_usage(second)
    druks_db.flush()

    assert UsageScrape.latest_for("claude", first.account_id).five_hour_percent_left == 84
    assert UsageScrape.latest_for("claude", second.account_id).five_hour_percent_left == 30
