from datetime import datetime

from pydantic import Field

from druks.schemas import BaseResponse


class UsageMetricSummary(BaseResponse):
    percent_left: int | None = None
    resets_at: datetime | None = None


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
    week: UsageMetricSummary | None = None
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


class UsageHarnessHistory(BaseResponse):
    name: str
    # Percent-left samples, oldest first. ``five_hour`` covers the last
    # ~6h (one full 5h window plus headroom); ``week`` covers the last
    # 7 days, downsampled. Either list is empty when the harness never
    # reported that window.
    five_hour: list[UsageHistoryPoint] = Field(default_factory=list)
    week: list[UsageHistoryPoint] = Field(default_factory=list)


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


class AgentHarnessUsage(BaseResponse):
    # One harness's quota for the agent surface: the latest snapshot's facts
    # plus a short percent-left trend per window, oldest first.
    name: str
    is_connected: bool = False
    plan_tier: str | None = None
    five_hour_percent_left: int | None = None
    five_hour_resets_at: datetime | None = None
    week_percent_left: int | None = None
    week_resets_at: datetime | None = None
    is_unlimited: bool = False
    scraped_at: datetime | None = None
    five_hour_history: list[UsageHistoryPoint] = Field(default_factory=list)
    week_history: list[UsageHistoryPoint] = Field(default_factory=list)


class AgentUsage(BaseResponse):
    # The caller's spend for the operator-local day plus per-harness quota.
    day: str
    timezone: str
    spend_today_usd: float
    tokens_today: int
    runs_today: int
    harnesses: list[AgentHarnessUsage] = Field(default_factory=list)
