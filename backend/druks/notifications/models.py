import secrets
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from druks.core.models import Uuid7Pk
from druks.models import Base
from druks.notifications.datastructures import NotificationState
from druks.notifications.exceptions import InvalidChoiceError


def mint_correlation_token() -> str:
    # 256 bits, URL-safe. The Notification.correlation_token column default —
    # every row gets one at flush, the way Uuid7Pk mints the id.
    return secrets.token_urlsafe(32)


class Notification(Base, Uuid7Pk):
    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(conversation_id, account_id) = 1",
            name="notifications_one_target_check",
        ),
        Index("notifications_account_idx", "account_id", "created_at"),
    )

    # The subject ({type, id}) of the run the notification is about. None when that run
    # has no subject, or when the notification is about no run.
    subject: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    # Why it fired — a machine category (e.g. "gate.parked"); the discriminator
    # for per-reason routing later.
    reason: Mapped[str]
    # Plain prose written by the caller — never a template.
    body: Mapped[str]
    # The offered choices [{id, label}]; None for a one-way notification.
    actions: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, default=None)
    # Reply-routing back to a parked run: (run_id, run_parked_at) is the exact
    # round a click answers — stored on the row because a run can re-park a new
    # round before the human acts.
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("durable_runs.id", ondelete="SET NULL"), default=None
    )
    run_parked_at: Mapped[datetime | None] = mapped_column(default=None)
    # A human view-link; the buttons carry the respond capability, this never does.
    deep_link: Mapped[str | None] = mapped_column(default=None)
    # A run that a Slack conversation started posts its buttons in that conversation.
    conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("chat_conversations.id", ondelete="CASCADE"), default=None
    )
    # The account that the notification is for. Only that account reads it in Druks.
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), default=None
    )
    # Minted at flush like the id above — the respond capability handle.
    correlation_token: Mapped[str] = mapped_column(
        String, unique=True, default=mint_correlation_token
    )
    state: Mapped[str] = mapped_column(default=NotificationState.PENDING.value)
    # The posted message's id, as the service that posted it names it.
    message_id: Mapped[str | None] = mapped_column(default=None)
    # How many times deliver() ran for this row, across outbox retries.
    attempts: Mapped[int] = mapped_column(default=0)
    # Sanitized only — this lands on API responses.
    last_error: Mapped[str | None] = mapped_column(default=None)
    delivered_at: Mapped[datetime | None] = mapped_column(default=None)
    # When the person marked the notification read.
    read_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    @classmethod
    async def create(
        cls,
        session: AsyncSession,
        *,
        conversation_id: str | None = None,
        account_id: str | None = None,
        reason: str,
        body: str,
        subject: dict[str, Any] | None = None,
        actions: list[dict[str, Any]] | None = None,
        run_id: str | None = None,
        run_parked_at: datetime | None = None,
        deep_link: str | None = None,
    ) -> "Notification":
        # The create seam: persist the row (token minted by the column default),
        # no enqueue — so a workflow step can call it and the body enqueues the
        # outbox afterwards.
        notification = cls(
            conversation_id=conversation_id,
            account_id=account_id,
            reason=reason,
            body=body,
            subject=subject,
            actions=actions,
            run_id=run_id,
            run_parked_at=run_parked_at,
            deep_link=deep_link,
        )
        session.add(notification)
        await session.flush()
        return notification

    @classmethod
    async def list_for_account(
        cls,
        session: AsyncSession,
        account_id: str,
        *,
        reason: str = "",
        since: datetime | None = None,
        kinds: Sequence[str] | None = None,
        limit: int = 50,
    ) -> list["Notification"]:
        """The notifications sent to ``account_id``, newest first: all of them, or those
        with ``reason`` that were made at or after ``since``. With ``kinds``, a
        notification about a run counts only when the run is of those workflows."""
        stmt = select(cls).where(cls.account_id == account_id)
        if reason:
            stmt = stmt.where(cls.reason == reason)
        if since:
            stmt = stmt.where(cls.created_at >= since)
        if kinds is not None:  # ast-grep-ignore: rule-2
            # The durable models import this module, so this import cannot be at the top.
            from druks.durable.models import Run  # noqa: PLC0415

            stmt = stmt.outerjoin(Run, Run.id == cls.run_id).where(
                or_(cls.run_id.is_(None), Run.kind.in_(kinds))
            )
        # uuid7 ids are time-ordered, breaking created_at ties toward the newest.
        stmt = stmt.order_by(cls.created_at.desc(), cls.id.desc()).limit(limit)
        return list(await session.scalars(stmt))

    @classmethod
    async def mark_all_read(cls, session: AsyncSession, account_id: str) -> None:
        """Mark every unread notification sent to ``account_id`` read."""
        await session.execute(
            update(cls)
            .where(cls.account_id == account_id, cls.read_at.is_(None))
            .values(read_at=Base.utc_now(), updated_at=Base.utc_now())
        )

    @classmethod
    async def get_for_token(cls, session: AsyncSession, token: str) -> "Notification | None":
        return (
            await session.execute(select(cls).where(cls.correlation_token == token))
        ).scalar_one_or_none()

    @classmethod
    async def list_open(
        cls, session: AsyncSession, run_id: str, parked_at: datetime
    ) -> list["Notification"]:
        """The posted messages that still show buttons for the question the run parked on
        at ``parked_at``."""
        stmt = select(cls).where(
            cls.run_id == run_id,
            cls.run_parked_at == parked_at,
            cls.state == NotificationState.DELIVERED.value,
            cls.message_id.is_not(None),
            # A message without buttons stores JSON null, which is not SQL NULL.
            cls.actions != JSONB.NULL,
        )
        return list(await session.scalars(stmt))

    @property
    def is_acknowledged(self) -> bool:
        return self.state == NotificationState.ACKNOWLEDGED

    def get_action(self, action_id: str) -> dict[str, Any]:
        """The offered choice that a click on ``action_id`` picks."""
        for action in self.actions:
            if action["id"] == action_id:
                return action
        raise InvalidChoiceError(f"unknown control {action_id!r}")

    async def mark_delivered(self, message_id: str | None = None) -> None:
        self.message_id = message_id
        self.state = NotificationState.DELIVERED.value
        self.delivered_at = Base.utc_now()
        self.updated_at = Base.utc_now()
        await self.session.flush()

    async def mark_failed(self, reason: str) -> None:
        self.state = NotificationState.FAILED.value
        self.last_error = reason
        self.updated_at = Base.utc_now()
        await self.session.flush()

    async def mark_acknowledged(self) -> bool:
        # Atomic claim: exactly one concurrent responder wins the transition
        # (the loser's duplicate send already collapsed on the DBOS round key).
        claimed = await self.session.execute(
            update(Notification)
            .where(
                Notification.id == self.id,
                Notification.state != NotificationState.ACKNOWLEDGED.value,
            )
            .values(state=NotificationState.ACKNOWLEDGED.value, updated_at=Base.utc_now())
        )
        await self.session.refresh(self)
        return claimed.rowcount == 1
