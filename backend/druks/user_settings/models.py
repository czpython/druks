from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import String
from sqlalchemy_encrypted_field import EncryptedTextField

from druks.models import Base

from .constants import (
    DEFAULT_BILLING,
    DEFAULT_EFFORT,
    DEFAULT_HARNESS,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
)
from .datastructures import ResolvedChoice, ResolvedTimeout


class InstallationSettings(Base):
    """Shared execution settings and the notification default for new accounts."""

    __tablename__ = "settings"
    __table_args__ = (CheckConstraint("id = 1", name="settings_singleton"),)

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    default_harness: Mapped[str] = mapped_column(String, default=DEFAULT_HARNESS)
    default_model: Mapped[str] = mapped_column(String, default=DEFAULT_MODEL)
    default_billing: Mapped[str] = mapped_column(String, default=DEFAULT_BILLING)
    default_effort: Mapped[str] = mapped_column(String, default=DEFAULT_EFFORT)
    fast_mode: Mapped[bool] = mapped_column(default=False)
    default_timeout: Mapped[int] = mapped_column(default=DEFAULT_TIMEOUT)
    gate_park_destination_id: Mapped[str | None] = mapped_column(
        ForeignKey("notification_destinations.id", ondelete="SET NULL"), default=None
    )
    updated_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    @classmethod
    async def get(cls, session: AsyncSession) -> "InstallationSettings":
        """Read or create the installation settings."""
        query = select(cls).where(cls.id == 1)
        if row := await session.scalar(query):
            return row
        await session.execute(
            pg_insert(cls).values(id=1).on_conflict_do_nothing(index_elements=["id"])
        )
        return (await session.scalars(query)).one()

    async def update(self, **fields: object) -> None:
        for field, value in fields.items():
            setattr(self, field, value)
        self.updated_at = Base.utc_now()
        await self.session.flush()


class SettingsOverride(Base):
    __tablename__ = "settings_overrides"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB(none_as_null=True), nullable=True)
    secret_value = EncryptedTextField(default="")

    @classmethod
    async def read(cls, session: AsyncSession, key: str) -> Any | None:
        row = await session.get(cls, key)
        return row.value if row else None

    @classmethod
    async def write(cls, session: AsyncSession, key: str, value: Any) -> None:
        row = await session.get(cls, key)
        if value is None:
            if row:
                await session.delete(row)
        elif row:
            row.value = value
        else:
            session.add(cls(key=key, value=value))
        await session.flush()

    @classmethod
    async def agent_harness(
        cls, session: AsyncSession, name: str, *, settings: InstallationSettings
    ) -> ResolvedChoice:
        override = await cls.read(session, f"agent_harness:{name}")
        if override:
            return ResolvedChoice(override, "agent")
        return ResolvedChoice(settings.default_harness, "default")

    @classmethod
    async def set_agent_harness(cls, session: AsyncSession, name: str, harness: str | None) -> None:
        await cls.write(session, f"agent_harness:{name}", harness)

    @classmethod
    async def agent_model(
        cls, session: AsyncSession, name: str, *, settings: InstallationSettings
    ) -> ResolvedChoice:
        override = await cls.read(session, f"agent_model:{name}")
        if override:
            return ResolvedChoice(override, "agent")
        return ResolvedChoice(settings.default_model, "default")

    @classmethod
    async def set_agent_model(cls, session: AsyncSession, name: str, model: str | None) -> None:
        await cls.write(session, f"agent_model:{name}", model)

    @classmethod
    async def agent_billing(
        cls, session: AsyncSession, name: str, *, settings: InstallationSettings
    ) -> ResolvedChoice:
        override = await cls.read(session, f"agent_billing:{name}")
        if override:
            return ResolvedChoice(override, "agent")
        return ResolvedChoice(settings.default_billing, "default")

    @classmethod
    async def set_agent_billing(cls, session: AsyncSession, name: str, billing: str | None) -> None:
        await cls.write(session, f"agent_billing:{name}", billing)

    @classmethod
    async def agent_effort(
        cls, session: AsyncSession, name: str, *, settings: InstallationSettings
    ) -> ResolvedChoice:
        override = await cls.read(session, f"agent_effort:{name}")
        if override:
            return ResolvedChoice(override, "agent")
        return ResolvedChoice(settings.default_effort, "default")

    @classmethod
    async def set_agent_effort(cls, session: AsyncSession, name: str, value: str | None) -> None:
        await cls.write(session, f"agent_effort:{name}", value)

    @classmethod
    async def agent_timeout(
        cls,
        session: AsyncSession,
        name: str,
        declared: int | None,
        *,
        settings: InstallationSettings,
    ) -> ResolvedTimeout:
        override = await cls.read(session, f"agent_timeout:{name}")
        if override:
            return ResolvedTimeout(override, "agent")
        if declared:
            return ResolvedTimeout(declared, "declared")
        return ResolvedTimeout(settings.default_timeout, "default")

    @classmethod
    async def set_agent_timeout(cls, session: AsyncSession, name: str, value: int | None) -> None:
        await cls.write(session, f"agent_timeout:{name}", value)

    @classmethod
    async def workflow_setting(
        cls, session: AsyncSession, kind: str, field: str, default: Any
    ) -> Any:
        value = await cls.read(session, f"workflow:{kind}:{field}")
        return default if value is None else value

    @classmethod
    async def set_workflow_setting(
        cls, session: AsyncSession, kind: str, field: str, value: Any
    ) -> None:
        await cls.write(session, f"workflow:{kind}:{field}", value)

    @classmethod
    async def app_setting(
        cls, session: AsyncSession, app: str, field: str, default: Any, *, is_secret: bool
    ) -> Any:
        row = await session.get(cls, f"app:{app}:{field}")
        if is_secret:
            return row.secret_value.decrypt() if row and row.secret_value else default
        return row.value if row else default

    @classmethod
    async def set_app_setting(
        cls, session: AsyncSession, app: str, field: str, value: Any, *, is_secret: bool
    ) -> None:
        key = f"app:{app}:{field}"
        if value is None or not is_secret:
            await cls.write(session, key, value)
            return
        row = await session.get(cls, key)
        if row:
            row.value = None
            row.secret_value = value
        else:
            row = cls(key=key, value=None, secret_value=value)
            session.add(row)
        await session.flush()
        # Assignment leaves the plaintext str on the instance; reload it now so
        # the next read sees the envelope — an expired attribute can't
        # lazy-load under the async session.
        await session.refresh(row)
