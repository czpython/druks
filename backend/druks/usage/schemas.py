from datetime import datetime

from pydantic import Field

from druks.schemas import BaseResponse


class UsageMetricSummary(BaseResponse):
    percent_left: int | None = None
    resets_at: datetime | None = None
    # Set when this window meters one model separately from the rest,
    # naming it. None covers every model.
    model: str | None = None


class UsageHarnessSummary(BaseResponse):
    # A registered harness name (get_harnesses()) — the UI keys panels,
    # colors, and legends off it.
    name: str
    # True when we have any usable percentage. False covers both "no
    # snapshot yet" (fresh install pre-first-poll) and "all parses
    # failed in the last snapshot".
    available: bool
    # False renders the connect action — the account has no connection
    # for this harness.
    connected: bool
    plan_tier: str | None = None
    five_hour: UsageMetricSummary | None = None
    weeks: list[UsageMetricSummary] = Field(default_factory=list)
    # Unmetered plan (Codex business/enterprise). The window buckets are
    # synthesized permanently-full — the UI shows "unmetered" plus
    # actual consumption from druks' own run records instead of a
    # quota bar that never moves.
    unlimited: bool = False
    scraped_at: datetime | None = None
    # Seconds since the snapshot was persisted. None when ``available``
    # is False from "no row yet".
    age_seconds: int | None = None
    # True once a row is >24h old. The pill switches to a warning
    # glyph and the panel surfaces a "scraper hasn't run" message.
    stale: bool = False
    # Short tag from the snapshot: "auth_required" / "not_installed"
    # / "parse_failed" / "timeout" / "crashed". Drives the panel's
    # disclosure copy.
    error: str | None = None
    # Raw captured output — only included in the panel detail view so
    # the operator can debug a parse failure. Empty string when no
    # snapshot exists. Truncated server-side to the last 8KB.
    raw_output: str | None = None


class UsageResponse(BaseResponse):
    # One summary per registered harness, in registry order.
    harnesses: list[UsageHarnessSummary]


class UsageHistoryPoint(BaseResponse):
    t: datetime
    pct: int


class UsageWindowHistory(BaseResponse):
    model: str | None = None
    points: list[UsageHistoryPoint] = Field(default_factory=list)


class UsageHarnessHistory(BaseResponse):
    name: str
    # Percent-left samples, oldest first. ``five_hour`` covers the last
    # ~6h (one full 5h window plus headroom); ``weeks`` covers the last
    # 7 days as one downsampled series per weekly window. Either list is
    # empty when the harness never reported that window.
    five_hour: list[UsageHistoryPoint] = Field(default_factory=list)
    weeks: list[UsageWindowHistory] = Field(default_factory=list)


class UsageHistoryResponse(BaseResponse):
    harnesses: list[UsageHarnessHistory]


class UsageHarnessToday(BaseResponse):
    name: str
    spend_usd: float
    tokens: int
    runs: int
    # Spend per local hour (24 buckets) for the histogram.
    hours: list[float]


class UsageTodayResponse(BaseResponse):
    # Local day the aggregates cover — same boundary as the sys-strip's
    # spend-today figure (operator timezone, finished_at attribution).
    day: str
    timezone: str
    harnesses: list[UsageHarnessToday]
