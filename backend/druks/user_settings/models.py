from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import String

from druks.database import db_session
from druks.models import Base
from druks.secrets.fields import EncryptedTextField

from .constants import (
    DEFAULT_BILLING,
    DEFAULT_EFFORT,
    DEFAULT_HARNESS,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
)
from .datastructures import ResolvedChoice, ResolvedTimeout


class UserSettings(Base):
    __tablename__ = "user_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    timezone: Mapped[str] = mapped_column(String, default="UTC")
    default_harness: Mapped[str] = mapped_column(String, default=DEFAULT_HARNESS)
    default_model: Mapped[str] = mapped_column(String, default=DEFAULT_MODEL)
    default_billing: Mapped[str] = mapped_column(String, default=DEFAULT_BILLING)
    default_effort: Mapped[str] = mapped_column(String, default=DEFAULT_EFFORT)
    fast_mode: Mapped[bool] = mapped_column(default=False)
    default_timeout: Mapped[int] = mapped_column(default=DEFAULT_TIMEOUT)
    # The designated gate-park notification destination; unset — or the
    # destination deleted (SET NULL) — turns gate-park notifications off.
    gate_park_destination_id: Mapped[str | None] = mapped_column(
        ForeignKey("notification_destinations.id", ondelete="SET NULL"), default=None
    )
    # Who an unattended run (a webhook, a schedule) runs as; the first
    # subscription sets it.
    fallback_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), default=None
    )
    updated_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    SINGLETON_ID = 1

    @classmethod
    async def get(cls) -> "UserSettings":
        session = db_session()
        row = await session.get(cls, cls.SINGLETON_ID)
        if not row:
            await session.execute(
                pg_insert(cls).values(id=cls.SINGLETON_ID).on_conflict_do_nothing()
            )
            row = await session.get_one(cls, cls.SINGLETON_ID)
        return row

    async def update_profile(self, **fields: object) -> None:
        for field, value in fields.items():
            setattr(self, field, value)
        self.updated_at = Base.utc_now()
        await db_session().flush()

    async def set_fallback_account(self, account_id: str) -> None:
        self.fallback_account_id = account_id
        self.updated_at = Base.utc_now()
        await db_session().flush()

    async def set_gate_park_destination(self, destination_id: str | None) -> None:
        # None is the off-switch, so this is a set-or-clear, not a skip-on-None.
        self.gate_park_destination_id = destination_id
        self.updated_at = Base.utc_now()
        await db_session().flush()


class SettingsOverride(Base):
    __tablename__ = "settings_overrides"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB(none_as_null=True), nullable=True)
    secret_value = EncryptedTextField(default="")

    @classmethod
    async def read(cls, key: str) -> Any | None:
        row = await db_session().get(cls, key)
        return row.value if row else None

    @classmethod
    async def write(cls, key: str, value: Any) -> None:
        session = db_session()
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
    async def agent_harness(cls, name: str) -> ResolvedChoice:
        override = await cls.read(f"agent_harness:{name}")
        if override:
            return ResolvedChoice(override, "agent")
        return ResolvedChoice((await UserSettings.get()).default_harness, "default")

    @classmethod
    async def set_agent_harness(cls, name: str, harness: str | None) -> None:
        await cls.write(f"agent_harness:{name}", harness)

    @classmethod
    async def agent_model(cls, name: str) -> ResolvedChoice:
        override = await cls.read(f"agent_model:{name}")
        if override:
            return ResolvedChoice(override, "agent")
        return ResolvedChoice((await UserSettings.get()).default_model, "default")

    @classmethod
    async def set_agent_model(cls, name: str, model: str | None) -> None:
        await cls.write(f"agent_model:{name}", model)

    @classmethod
    async def agent_billing(cls, name: str) -> ResolvedChoice:
        override = await cls.read(f"agent_billing:{name}")
        if override:
            return ResolvedChoice(override, "agent")
        return ResolvedChoice((await UserSettings.get()).default_billing, "default")

    @classmethod
    async def set_agent_billing(cls, name: str, billing: str | None) -> None:
        await cls.write(f"agent_billing:{name}", billing)

    @classmethod
    async def agent_effort(cls, name: str) -> ResolvedChoice:
        override = await cls.read(f"agent_effort:{name}")
        if override:
            return ResolvedChoice(override, "agent")
        return ResolvedChoice((await UserSettings.get()).default_effort, "default")

    @classmethod
    async def set_agent_effort(cls, name: str, value: str | None) -> None:
        await cls.write(f"agent_effort:{name}", value)

    @classmethod
    async def agent_timeout(cls, name: str, declared: int | None) -> ResolvedTimeout:
        override = await cls.read(f"agent_timeout:{name}")
        if override:
            return ResolvedTimeout(override, "agent")
        if declared:
            return ResolvedTimeout(declared, "declared")
        return ResolvedTimeout((await UserSettings.get()).default_timeout, "default")

    @classmethod
    async def set_agent_timeout(cls, name: str, value: int | None) -> None:
        await cls.write(f"agent_timeout:{name}", value)

    @classmethod
    async def workflow_setting(cls, kind: str, field: str, default: Any) -> Any:
        value = await cls.read(f"workflow:{kind}:{field}")
        return default if value is None else value

    @classmethod
    async def set_workflow_setting(cls, kind: str, field: str, value: Any) -> None:
        await cls.write(f"workflow:{kind}:{field}", value)

    @classmethod
    async def app_setting(cls, app: str, field: str, default: Any, *, is_secret: bool) -> Any:
        row = await db_session().get(cls, f"app:{app}:{field}")
        if is_secret:
            return row.secret_value.decrypt() if row and row.secret_value else default
        return row.value if row else default

    @classmethod
    async def set_app_setting(cls, app: str, field: str, value: Any, *, is_secret: bool) -> None:
        key = f"app:{app}:{field}"
        if value is None or not is_secret:
            await cls.write(key, value)
            return
        session = db_session()
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
