import secrets
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Boolean, ForeignKey, String, select, true, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from druks.core.models import Uuid7Pk
from druks.database import db_session
from druks.models import Base
from druks.notifications.datastructures import NotificationState
from druks.notifications.exceptions import UnknownDestinationKindError


def mint_correlation_token() -> str:
    # 256 bits, URL-safe. The Notification.correlation_token column default —
    # every row gets one at flush, the way Uuid7Pk mints the id.
    return secrets.token_urlsafe(32)


class DestinationKind(StrEnum):
    SLACK_WEBHOOK = "slack_webhook"


class Destination(Base, Uuid7Pk):
    __tablename__ = "notification_destinations"

    name: Mapped[str] = mapped_column(String, unique=True)
    kind: Mapped[str] = mapped_column(String)
    # The webhook URL is the credential and is plaintext at rest; it leaves the
    # row in the clear only inside deliver().
    url: Mapped[str] = mapped_column(String)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    @classmethod
    async def list_all(cls) -> list["Destination"]:
        return list((await db_session().execute(select(cls).order_by(cls.name))).scalars())

    @classmethod
    async def get(cls, destination_id: str) -> "Destination | None":
        return await db_session().get(cls, destination_id)

    @classmethod
    async def get_for_name(cls, name: str) -> "Destination | None":
        return (
            await db_session().execute(select(cls).where(cls.name == name))
        ).scalar_one_or_none()

    @classmethod
    async def create(cls, *, name: str, kind: str, url: str) -> "Destination":
        try:
            DestinationKind(kind)
        except ValueError as error:
            raise UnknownDestinationKindError(kind, tuple(DestinationKind)) from error
        session = db_session()
        destination = cls(name=name, kind=kind, url=url)
        session.add(destination)
        await session.flush()
        return destination

    async def delete(self) -> None:
        session = db_session()
        await session.delete(self)
        await session.flush()


class Notification(Base, Uuid7Pk):
    __tablename__ = "notifications"

    # What the notification is about ({type, id}) — always present: every
    # notification is about a run's subject (subjectless parks never notify).
    subject: Mapped[dict[str, Any]] = mapped_column(JSONB)
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
    destination_id: Mapped[str] = mapped_column(ForeignKey("notification_destinations.id"))
    # Minted at flush like the id above — the respond capability handle.
    correlation_token: Mapped[str] = mapped_column(
        String, unique=True, default=mint_correlation_token
    )
    state: Mapped[str] = mapped_column(default=NotificationState.PENDING.value)
    # How many times deliver() ran for this row, across outbox retries.
    attempts: Mapped[int] = mapped_column(default=0)
    # Sanitized only — this lands on API responses.
    last_error: Mapped[str | None] = mapped_column(default=None)
    delivered_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    @classmethod
    async def create(
        cls,
        *,
        destination_id: str,
        reason: str,
        body: str,
        subject: dict[str, Any],
        actions: list[dict[str, Any]] | None = None,
        run_id: str | None = None,
        run_parked_at: datetime | None = None,
        deep_link: str | None = None,
    ) -> "Notification":
        # The create seam: persist the row (token minted by the column default),
        # no enqueue — so a workflow step can call it and the body enqueues the
        # outbox afterwards.
        notification = cls(
            destination_id=destination_id,
            reason=reason,
            body=body,
            subject=subject,
            actions=actions,
            run_id=run_id,
            run_parked_at=run_parked_at,
            deep_link=deep_link,
        )
        session = db_session()
        session.add(notification)
        await session.flush()
        return notification

    @classmethod
    async def get(cls, notification_id: str) -> "Notification | None":
        return await db_session().get(cls, notification_id)

    @classmethod
    async def list_recent(cls, limit: int = 50) -> list["Notification"]:
        # uuid7 ids are time-ordered, breaking created_at ties toward the newest.
        stmt = select(cls).order_by(cls.created_at.desc(), cls.id.desc()).limit(limit)
        return list(await db_session().scalars(stmt))

    @classmethod
    async def get_for_token(cls, token: str) -> "Notification | None":
        return (
            await db_session().execute(select(cls).where(cls.correlation_token == token))
        ).scalar_one_or_none()

    @property
    def is_acknowledged(self) -> bool:
        return self.state == NotificationState.ACKNOWLEDGED

    async def mark_delivered(self) -> None:
        self.state = NotificationState.DELIVERED.value
        self.delivered_at = Base.utc_now()
        self.updated_at = Base.utc_now()
        await db_session().flush()

    async def mark_failed(self, reason: str) -> None:
        self.state = NotificationState.FAILED.value
        self.last_error = reason
        self.updated_at = Base.utc_now()
        await db_session().flush()

    async def mark_acknowledged(self) -> bool:
        # Atomic claim: exactly one concurrent responder wins the transition
        # (the loser's duplicate send already collapsed on the DBOS round key).
        session = db_session()
        claimed = await session.execute(
            update(Notification)
            .where(
                Notification.id == self.id,
                Notification.state != NotificationState.ACKNOWLEDGED.value,
            )
            .values(state=NotificationState.ACKNOWLEDGED.value, updated_at=Base.utc_now())
        )
        await session.refresh(self)
        return claimed.rowcount == 1
