from datetime import UTC, datetime, timedelta

from croniter import croniter
from fastapi import APIRouter, status
from sqlalchemy import select

from druks.accounts.dependencies import CurrentAccountDep
from druks.core.utils.time import operator_local_day
from druks.db import db_session
from druks.harnesses.artifacts import normalize_token_usage
from druks.harnesses.registry import get_harnesses
from druks.usage.models import UsageScrape
from druks.usage.schemas import (
    UsageHarnessHistory,
    UsageHarnessSummary,
    UsageHarnessToday,
    UsageHistoryPoint,
    UsageHistoryResponse,
    UsageMetricSummary,
    UsageResponse,
    UsageTodayResponse,
)
from druks.usage.workflows import PollUsage
from druks.user_settings.models import UserSettings
from druks.workflows import AgentCall

router = APIRouter(tags=["usage"])

# The /today bucket for calls whose model no current harness claims.
UNATTRIBUTED = "unattributed"

# When a snapshot crosses this age, the pill flips to a warning glyph
# and the panel surfaces "scraper hasn't run in a while". Tunable but
# 24h is a reasonable "yeah that's actually broken" threshold given the
# default 5-min poll cadence.
_STALE_AFTER_SECONDS = 24 * 60 * 60

# Trend ranges for the usage page sparklines. The 5h window gets one
# full window plus headroom so an exhaustion arc is visible end to end;
# weekly gets the whole week. Both are downsampled to keep the payload
# flat regardless of poll cadence.
_FIVE_HOUR_RANGE = timedelta(hours=6)
_WEEK_RANGE = timedelta(days=7)
_MAX_SPARK_POINTS = 72


@router.get(
    "",
    response_model=UsageResponse,
    response_model_by_alias=True,
)
async def get_usage() -> UsageResponse:
    now = datetime.now(UTC)
    return UsageResponse(
        harnesses=[
            _summarize(UsageScrape.latest_for(h.name), name=h.name, now=now)
            for h in get_harnesses()
        ],
        polling_enabled=PollUsage.has_enabled_schedule(),
        polling_interval_seconds=_poll_interval_seconds(now),
    )


@router.post("/refresh", status_code=status.HTTP_202_ACCEPTED)
async def refresh_usage(account: CurrentAccountDep) -> None:
    # Fire-and-forget: the frontend polls GET /api/usage for the new row.
    await PollUsage.start(subject=None, account_id=account.id)


@router.get(
    "/history",
    response_model=UsageHistoryResponse,
    response_model_by_alias=True,
)
async def get_usage_history() -> UsageHistoryResponse:
    now = datetime.now(UTC)
    return UsageHistoryResponse(
        harnesses=[_harness_history(h.name, now=now) for h in get_harnesses()],
    )


@router.get(
    "/today",
    response_model=UsageTodayResponse,
    response_model_by_alias=True,
)
async def get_usage_today() -> UsageTodayResponse:
    # The shared operator-local-day boundary keeps this total identical to
    # the sys-strip's spend-today figure.
    timezone_name = UserSettings.get().timezone
    now = datetime.now(UTC)
    timezone, local_start = operator_local_day(timezone_name, now)
    timezone_name = str(timezone)
    rows = (
        db_session()
        .execute(
            select(
                AgentCall.model,
                AgentCall.cost_usd,
                AgentCall.cost_metadata,
                AgentCall.finished_at,
            )
            .where(AgentCall.finished_at.is_not(None))
            .where(AgentCall.finished_at >= local_start.astimezone(UTC))
            .where(AgentCall.finished_at < (local_start + timedelta(days=1)).astimezone(UTC)),
        )
        .all()
    )

    # Every call counts, even one whose model no current harness claims (ids
    # churn on deploy, opus-4-7 → 4-8) or was never resolved: money spent must
    # not vanish from the display, and the strip's total_run_spend_between
    # counts them too. Unclaimed calls land in an extra "unattributed" entry —
    # the panel's per-harness cards look up by name and skip it, its grand
    # total sums the whole list.
    harness_by_model = {model: h.name for h in get_harnesses() for model in h.models}
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


def _harness_history(name: str, *, now: datetime) -> UsageHarnessHistory:
    rows = UsageScrape.history_for(name, since=now - _WEEK_RANGE)
    five_hour_cutoff = now - _FIVE_HOUR_RANGE
    five_hour = [
        UsageHistoryPoint(t=row.scraped_at, pct=row.five_hour_percent_left)
        for row in rows
        if row.five_hour_percent_left is not None and row.scraped_at >= five_hour_cutoff
    ]
    week = [
        UsageHistoryPoint(t=row.scraped_at, pct=row.week_percent_left)
        for row in rows
        if row.week_percent_left is not None
    ]
    return UsageHarnessHistory(
        name=name,
        five_hour=_downsample(five_hour),
        week=_downsample(week),
    )


def _downsample(points: list[UsageHistoryPoint]) -> list[UsageHistoryPoint]:
    """Thin a series to ≤ _MAX_SPARK_POINTS, always keeping the newest
    sample (the page's "now" anchor)."""
    if len(points) <= _MAX_SPARK_POINTS:
        return points
    stride = -(-len(points) // _MAX_SPARK_POINTS)  # ceil division
    thinned = points[::stride]
    if thinned[-1] is not points[-1]:
        thinned.append(points[-1])
    return thinned


def _summarize(
    row: UsageScrape | None,
    *,
    name: str,
    now: datetime,
) -> UsageHarnessSummary:
    if not row:
        return UsageHarnessSummary(name=name, available=False)
    age = _age_seconds(row.scraped_at, now=now)
    return UsageHarnessSummary(
        name=name,
        available=row.parse_ok,
        plan_tier=row.plan_tier,
        five_hour=_metric(row.five_hour_percent_left, row.five_hour_resets_at),
        week=_metric(row.week_percent_left, row.week_resets_at),
        unlimited=row.unlimited,
        scraped_at=row.scraped_at,
        age_seconds=age,
        stale=age is not None and age >= _STALE_AFTER_SECONDS,
        error=row.error,
        raw_output=row.raw_output,
    )


def _metric(percent_left: int | None, resets_at: datetime | None) -> UsageMetricSummary | None:
    if percent_left is None and resets_at is None:
        return None
    return UsageMetricSummary(percent_left=percent_left, resets_at=resets_at)


def _poll_interval_seconds(now: datetime) -> int:
    # The panel's "next scrape in ~Nm" hint: the gap between the schedule's
    # next two fires. The cron is always valid — the declared default is, and
    # operator overrides are validated at write.
    cron = PollUsage.get_schedule()
    assert cron is not None  # every= is declared on PollUsage
    fires = croniter(cron, now)
    first = fires.get_next(datetime)
    return int((fires.get_next(datetime) - first).total_seconds())


def _age_seconds(scraped_at: datetime | None, *, now: datetime) -> int | None:
    if not scraped_at:
        return None
    return max(0, int((now - scraped_at).total_seconds()))
