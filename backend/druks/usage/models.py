from datetime import datetime, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Index, delete, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from druks.db import Base, db_session

if TYPE_CHECKING:
    from druks.secrets.models import VaultSecret

_POLL_INTERVAL_MINUTES = (5, 10, 20, 40, 60)


class UsageScrape(Base):
    __tablename__ = "usage_scrapes"
    __table_args__ = (
        Index("usage_scrapes_account_provider_time_idx", "account_id", "provider", "scraped_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str]  # a registered provider id (get_providers())
    # The account this snapshot describes.
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    scraped_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    # True when at least one metric came out of the parser. False covers
    # both "scrape failed entirely" (timeout, not signed in, binary
    # missing) and "scrape ran but format changed and nothing matched".
    parse_ok: Mapped[bool] = mapped_column(default=True)
    raw_output: Mapped[str | None]
    # Short classification string: ``timeout`` | ``not_installed`` |
    # ``auth_required`` | ``parse_failed`` | ``unknown``. ``None`` when
    # the scrape parsed cleanly.
    error: Mapped[str | None]
    # Subscription tier when the CLI surfaces it (e.g. ``pro``, ``max``,
    # ``plus``). Display-only.
    plan_tier: Mapped[str | None]
    # The provider's five-hour rolling window.
    five_hour_percent_left: Mapped[int | None]
    five_hour_resets_at: Mapped[datetime | None]
    # Weekly windows in provider order, including separately metered models.
    weeks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    # Unmetered plan (Codex business/enterprise with unlimited credits).
    # The window percentages above are synthesized permanently-full
    # buckets when this is set — the UI renders "unmetered" instead of
    # a quota bar that never moves.
    unlimited: Mapped[bool] = mapped_column(default=False)

    @classmethod
    async def is_due(cls, subscription: "VaultSecret", *, now: datetime) -> bool:
        """Scrape history, completed calls, and window resets determine when a poll is due."""
        # Cycle: durable.models loads harnesses, whose providers load UsageScrape.
        from druks.durable.models import AgentCall

        stmt = (
            select(cls)
            .where(
                cls.provider == subscription.audience_name,
                cls.account_id == subscription.account_id,
            )
            .order_by(cls.scraped_at.desc(), cls.id.desc())
            .limit(len(_POLL_INTERVAL_MINUTES))
        )
        rows = list(await db_session().scalars(stmt))

        if not rows:
            return True
        latest_scrape = rows[0]
        finished_call = select(AgentCall.id).where(
            AgentCall.subscription_id == subscription.id,
            AgentCall.finished_at > latest_scrape.scraped_at,
        )

        if await db_session().scalar(select(finished_call.exists())):
            return True
        exhausted_reset = latest_scrape.soonest_reset_after(
            latest_scrape.scraped_at, exhausted_only=True
        )

        if exhausted_reset:
            return now >= exhausted_reset
        reset = latest_scrape.soonest_reset_after(latest_scrape.scraped_at)

        if reset and now >= reset:
            return True
        unchanged_pairs = 0

        for newer_scrape, older_scrape in pairwise(rows):
            if newer_scrape.quota != older_scrape.quota:
                break
            unchanged_pairs += 1
        interval = timedelta(minutes=_POLL_INTERVAL_MINUTES[unchanged_pairs])
        return now >= latest_scrape.scraped_at + interval

    @classmethod
    async def latest_for(cls, provider_id: str, account_id: str) -> "UsageScrape | None":
        stmt = (
            select(cls)
            .where(cls.provider == provider_id, cls.account_id == account_id)
            .order_by(cls.scraped_at.desc())
            .limit(1)
        )
        return (await db_session().execute(stmt)).scalar_one_or_none()

    @classmethod
    async def history_for(
        cls, provider_id: str, account_id: str, *, since: datetime
    ) -> list["UsageScrape"]:
        """The account's successful scrapes for ``provider_id`` since ``since``,
        oldest first. Feeds the usage page's trend sparklines / burn-rate
        math, so failed scrapes (no percentages) are excluded."""
        stmt = (
            select(cls)
            .where(cls.provider == provider_id, cls.account_id == account_id)
            .where(cls.scraped_at >= since)
            .where(cls.parse_ok.is_(True))
            .order_by(cls.scraped_at.asc())
        )
        return list((await db_session().execute(stmt)).scalars())

    @property
    def quota(self) -> tuple[str | None, int | None, list[int | None]]:
        return (
            self.error,
            self.five_hour_percent_left,
            [week["percent_left"] for week in self.weeks],
        )

    def binding_week(self) -> dict[str, Any] | None:
        """The window closest to exhaustion — whichever stops work first."""
        reported_windows = [week for week in self.weeks if week["percent_left"] is not None]
        if reported_windows:
            return min(reported_windows, key=lambda week: week["percent_left"])

    def soonest_reset_after(
        self, after: datetime, *, exhausted_only: bool = False
    ) -> datetime | None:
        resets = []
        if (
            self.five_hour_resets_at
            and self.five_hour_resets_at > after
            and (not exhausted_only or self.five_hour_percent_left == 0)
        ):
            resets.append(self.five_hour_resets_at)
        for week in self.weeks:
            if week["resets_at"] and (not exhausted_only or week["percent_left"] == 0):
                reset = datetime.fromisoformat(week["resets_at"])
                if reset > after:
                    resets.append(reset)
        if resets:
            return min(resets)

    async def save(self) -> None:
        if not self.scraped_at:
            self.scraped_at = Base.utc_now()
        session = db_session()
        session.add(self)
        await session.flush()

    @classmethod
    async def prune_older_than(cls, *, days: int) -> int:
        cutoff = Base.utc_now() - timedelta(days=days)
        stmt = delete(cls).where(cls.scraped_at < cutoff)
        session = db_session()
        result = await session.execute(stmt)
        await session.flush()
        return result.rowcount
