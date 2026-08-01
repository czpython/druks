from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import ForeignKey, Index, delete, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from druks.db import Base, db_session


class UsageScrape(Base):
    __tablename__ = "usage_scrapes"
    __table_args__ = (
        Index("usage_scrapes_account_harness_time_idx", "account_id", "harness", "scraped_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    harness: Mapped[str]  # a registered harness name (get_harnesses())
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
    # Five-hour rolling window. Claude exposes this directly; Codex
    # doesn't have a 5h concept yet so it stays null for the codex row.
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
    def latest_for(cls, harness: str, account_id: str) -> "UsageScrape | None":
        stmt = (
            select(cls)
            .where(cls.harness == harness, cls.account_id == account_id)
            .order_by(cls.scraped_at.desc())
            .limit(1)
        )
        return db_session().execute(stmt).scalar_one_or_none()

    @classmethod
    def history_for(cls, harness: str, account_id: str, *, since: datetime) -> list["UsageScrape"]:
        """The account's successful scrapes for ``harness`` since ``since``,
        oldest first. Feeds the usage page's trend sparklines / burn-rate
        math, so failed scrapes (no percentages) are excluded."""
        stmt = (
            select(cls)
            .where(cls.harness == harness, cls.account_id == account_id)
            .where(cls.scraped_at >= since)
            .where(cls.parse_ok.is_(True))
            .order_by(cls.scraped_at.asc())
        )
        return list(db_session().execute(stmt).scalars())

    def binding_week(self) -> dict[str, Any] | None:
        """The window closest to exhaustion — whichever stops work first."""
        reported_windows = [week for week in self.weeks if week["percent_left"] is not None]
        if reported_windows:
            return min(reported_windows, key=lambda week: week["percent_left"])

    def soonest_reset_after(self, now: datetime) -> datetime | None:
        resets = []
        if self.five_hour_resets_at and self.five_hour_resets_at > now:
            resets.append(self.five_hour_resets_at)
        for week in self.weeks:
            if week["resets_at"]:
                reset = datetime.fromisoformat(week["resets_at"])
                if reset > now:
                    resets.append(reset)
        if resets:
            return min(resets)

    def save(self) -> None:
        if not self.scraped_at:
            self.scraped_at = Base.utc_now()
        session = db_session()
        session.add(self)
        session.flush()

    @classmethod
    def prune_older_than(cls, *, days: int) -> int:
        cutoff = Base.utc_now() - timedelta(days=days)
        stmt = delete(cls).where(cls.scraped_at < cutoff)
        session = db_session()
        result = session.execute(stmt)
        session.flush()
        return result.rowcount
