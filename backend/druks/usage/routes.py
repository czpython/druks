from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends

from druks.accounts.dependencies import current_account
from druks.accounts.models import Account
from druks.core.utils.time import operator_local_day
from druks.harnesses.artifacts import normalize_token_usage
from druks.harnesses.models import HarnessConnection
from druks.harnesses.registry import get_harnesses
from druks.usage.models import UsageScrape
from druks.usage.reads import list_finished_calls
from druks.usage.schemas import (
    UsageHarnessHistory,
    UsageHarnessSummary,
    UsageHarnessToday,
    UsageHistoryPoint,
    UsageHistoryResponse,
    UsageMetricSummary,
    UsageResponse,
    UsageTodayResponse,
    UsageWindowHistory,
)
from druks.usage.trends import FIVE_HOUR_RANGE, WEEK_RANGE, downsample
from druks.user_settings.models import HarnessSettings, UserSettings

router = APIRouter()

# The /today bucket for calls whose model no current harness claims.
UNATTRIBUTED = "unattributed"

# An open tab must not hammer the providers.
_REFRESH_FLOOR_SECONDS = 60

# When a snapshot crosses this age, the pill flips to a warning glyph
# and the panel surfaces "scraper hasn't run in a while". Tunable but
# 24h is a reasonable "yeah that's actually broken" threshold given the
# default 5-min poll cadence.
_STALE_AFTER_SECONDS = 24 * 60 * 60

# The dashboard sparklines keep this many points regardless of poll cadence.
_MAX_SPARK_POINTS = 72


@router.get(
    "",
    response_model=UsageResponse,
    response_model_by_alias=True,
)
async def get_usage(account: Account = Depends(current_account)) -> UsageResponse:
    now = datetime.now(UTC)
    summaries = []
    for harness in get_harnesses():
        connection = await HarnessConnection.get_for_account(harness.name, account.id)
        summaries.append(
            _summarize(
                await UsageScrape.latest_for(harness.name, account.id),
                name=harness.name,
                now=now,
                connected=bool(connection),
                provider_email=connection.provider_email if connection else None,
                is_metered=connection.is_metered if connection else True,
            )
        )
    return UsageResponse(harnesses=summaries)


@router.post("/refresh")
async def refresh_usage(account: Account = Depends(current_account)) -> None:
    now = datetime.now(UTC)
    for harness in get_harnesses():
        connection = await HarnessConnection.get_for_account(harness.name, account.id)
        row = await UsageScrape.latest_for(harness.name, account.id)
        age = _age_seconds(row.scraped_at, now=now) if row else None
        if connection and connection.is_metered and (age is None or age >= _REFRESH_FLOOR_SECONDS):
            await harness.poll_usage(connection)


@router.get(
    "/history",
    response_model=UsageHistoryResponse,
    response_model_by_alias=True,
)
async def get_usage_history(account: Account = Depends(current_account)) -> UsageHistoryResponse:
    now = datetime.now(UTC)
    return UsageHistoryResponse(
        harnesses=[await _harness_history(h.name, account.id, now=now) for h in get_harnesses()],
    )


@router.get(
    "/today",
    response_model=UsageTodayResponse,
    response_model_by_alias=True,
)
async def get_usage_today(account: Account = Depends(current_account)) -> UsageTodayResponse:
    # Deriving the operator-local-day window here (the query just takes it) keeps
    # this total identical to the sys-strip's and the agent surface's figures.
    timezone, local_start = operator_local_day(
        (await UserSettings.get()).timezone, datetime.now(UTC)
    )
    rows = await list_finished_calls(
        account.id, since=local_start, until=local_start + timedelta(days=1)
    )
    timezone_name = str(timezone)

    # Every call counts, even one whose model no picker list claims (pinned
    # outside the list, or an id that churned): money spent must not vanish from
    # the display, and the strip's total_run_spend_between counts them too.
    # Unclaimed calls land in an extra "unattributed" entry — the panel's
    # per-harness cards look up by name and skip it, its grand total sums the
    # whole list.
    harness_by_model = {
        entry["id"]: settings.name
        for settings in await HarnessSettings.all()
        for entry in settings.allowed_models
    }
    names = [h.name for h in get_harnesses()]
    totals = {name: {"spend": 0.0, "tokens": 0, "runs": 0} for name in [*names, UNATTRIBUTED]}
    hours: dict[str, list[float]] = {name: [0.0] * 24 for name in [*names, UNATTRIBUTED]}
    for model, cost_usd, cost_metadata, finished_at in rows:
        name = harness_by_model.get(model, UNATTRIBUTED)
        bucket = totals[name]
        bucket["runs"] += 1
        usage = normalize_token_usage(cost_metadata)
        if usage:
            bucket["tokens"] += usage["total_tokens"]
        if cost_usd is not None:
            bucket["spend"] += cost_usd
            hours[name][finished_at.astimezone(timezone).hour] += cost_usd

    included = [*names, UNATTRIBUTED] if totals[UNATTRIBUTED]["runs"] else names
    return UsageTodayResponse(
        day=local_start.date().isoformat(),
        timezone=timezone_name,
        harnesses=[
            UsageHarnessToday(
                name=name,
                spend_usd=round(float(totals[name]["spend"]), 4),
                tokens=int(totals[name]["tokens"]),
                runs=int(totals[name]["runs"]),
                hours=[round(v, 4) for v in hours[name]],
            )
            for name in included
        ],
    )


async def _harness_history(name: str, account_id: str, *, now: datetime) -> UsageHarnessHistory:
    rows = await UsageScrape.history_for(name, account_id, since=now - WEEK_RANGE)
    five_hour_cutoff = now - FIVE_HOUR_RANGE
    five_hour = [
        UsageHistoryPoint(t=row.scraped_at, pct=row.five_hour_percent_left)
        for row in rows
        if row.five_hour_percent_left is not None and row.scraped_at >= five_hour_cutoff
    ]
    weekly_points: dict[str | None, list[UsageHistoryPoint]] = {}
    for row in rows:
        for week in row.weeks:
            if week["percent_left"] is not None:
                weekly_points.setdefault(week["model"], []).append(
                    UsageHistoryPoint(t=row.scraped_at, pct=week["percent_left"])
                )
    return UsageHarnessHistory(
        name=name,
        five_hour=downsample(five_hour, cap=_MAX_SPARK_POINTS),
        weeks=[
            UsageWindowHistory(
                model=model,
                points=downsample(points, cap=_MAX_SPARK_POINTS),
            )
            for model, points in weekly_points.items()
        ],
    )


def _summarize(
    row: UsageScrape | None,
    *,
    name: str,
    now: datetime,
    connected: bool,
    provider_email: str | None,
    is_metered: bool,
) -> UsageHarnessSummary:
    if connected and not is_metered:
        return UsageHarnessSummary(
            name=name,
            available=True,
            connected=True,
            provider_email=provider_email,
            unlimited=True,
        )
    if not row:
        return UsageHarnessSummary(
            name=name,
            available=False,
            connected=connected,
            provider_email=provider_email,
        )
    age = _age_seconds(row.scraped_at, now=now)
    five_hour = None
    if row.five_hour_percent_left is not None or row.five_hour_resets_at:
        five_hour = UsageMetricSummary(
            percent_left=row.five_hour_percent_left,
            resets_at=row.five_hour_resets_at,
        )
    return UsageHarnessSummary(
        name=name,
        available=row.parse_ok,
        connected=connected,
        provider_email=provider_email,
        plan_tier=row.plan_tier,
        five_hour=five_hour,
        weeks=[UsageMetricSummary.model_validate(week) for week in row.weeks],
        unlimited=row.unlimited,
        scraped_at=row.scraped_at,
        age_seconds=age,
        stale=age is not None and age >= _STALE_AFTER_SECONDS,
        error=row.error,
        raw_output=row.raw_output,
    )


def _age_seconds(scraped_at: datetime | None, *, now: datetime) -> int | None:
    if not scraped_at:
        return
    return max(0, int((now - scraped_at).total_seconds()))
