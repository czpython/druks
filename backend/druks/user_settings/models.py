import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import String

from druks.database import db_session
from druks.models import Base

from .datastructures import (
    ResolvedEffort,
    ResolvedModel,
    ResolvedTimeout,
)

if TYPE_CHECKING:
    from druks.harnesses.base import Harness
    from druks.harnesses.models import HarnessConnection

logger = logging.getLogger(__name__)


class UserSettings(Base):
    __tablename__ = "user_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    timezone: Mapped[str] = mapped_column(String, default="UTC")
    # The designated gate-park notification destination; unset — or the
    # destination deleted (SET NULL) — turns gate-park notifications off.
    gate_park_destination_id: Mapped[str | None] = mapped_column(
        ForeignKey("notification_destinations.id", ondelete="SET NULL"), default=None
    )
    # The account actor-less runs (webhooks, schedules) run as, until runs are
    # account-attributed; the very first login sets it.
    fallback_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), default=None
    )
    updated_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    SINGLETON_ID = 1

    @classmethod
    def get(cls) -> "UserSettings":
        session = db_session()
        row = session.get(cls, cls.SINGLETON_ID)
        if not row:
            session.execute(pg_insert(cls).values(id=cls.SINGLETON_ID).on_conflict_do_nothing())
            row = session.get_one(cls, cls.SINGLETON_ID)
        return row

    def update_profile(self, *, timezone: str | None = None) -> None:
        if timezone:
            self.timezone = timezone
        self.updated_at = Base.utc_now()
        db_session().flush()

    def set_fallback_account(self, account_id: str) -> None:
        self.fallback_account_id = account_id
        self.updated_at = Base.utc_now()
        db_session().flush()

    def set_gate_park_destination(self, destination_id: str | None) -> None:
        # None is the off-switch, so this is a set-or-clear, not a skip-on-None.
        self.gate_park_destination_id = destination_id
        self.updated_at = Base.utc_now()
        db_session().flush()


class HarnessSettings(Base):
    # One row per registered harness (claude, codex, …), seeded from the
    # registry on install and tuned by the operator. An agent inherits its
    # harness's model / effort / timeout / fast_mode unless it declares its own
    # or carries a per-agent override.
    __tablename__ = "harnesses"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    model: Mapped[str] = mapped_column(String)
    fast_mode: Mapped[bool] = mapped_column(default=False)
    effort: Mapped[str] = mapped_column(String, default="high")
    timeout: Mapped[int] = mapped_column(default=1800)
    updated_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    # Provider-fetched picker models (``{"id", "label", …}`` dicts); falsy ⇒
    # never fetched, the harness's shipped tuple serves.
    models_fetched: Mapped[Any] = mapped_column(JSONB, default=None, nullable=True)
    models_fetched_at: Mapped[datetime | None] = mapped_column(default=None)

    @classmethod
    def get(cls, name: str) -> "HarnessSettings | None":
        return db_session().get(cls, name)

    @classmethod
    def require(cls, name: str) -> "HarnessSettings":
        # The resolution paths (effort/timeout, the harness factory) only pass a
        # ``get_harness_for_model`` name, which is always registered and so always
        # seeded — a miss means ``seed_harnesses`` didn't run before serving.
        if not (config := cls.get(name)):
            raise KeyError(f"no harness settings for {name!r}; seed_harnesses missed it")
        return config

    @classmethod
    def all(cls) -> list["HarnessSettings"]:
        return list(db_session().execute(select(cls).order_by(cls.name)).scalars())

    @property
    def harness(self) -> "type[Harness]":
        from druks.harnesses.registry import get_harness

        return get_harness(self.name)

    @property
    def provider(self) -> str:
        return self.harness.provider

    @property
    def allowed_models(self) -> list[dict]:
        """Picker models, ``{"id", "label"}`` each — the provider-fetched
        list when one has landed, else the harness's shipped tuple."""
        if self.models_fetched:
            return [{"id": m["id"], "label": m["label"]} for m in self.models_fetched]
        return [{"id": name, "label": name} for name in self.harness.models]

    async def refresh_models(self, connection: "HarnessConnection") -> dict[str, object]:
        """Fetch the provider's selectable models over ``connection`` and store
        them on this row. Every failure is a tag in the report, never a raise,
        and leaves the stored list untouched — connect and the cron shrug it off."""
        try:
            parsed = await self.harness.fetch_models(connection)
        except Exception:  # noqa: BLE001 — a crashed fetch reports a tag, not a failed caller
            logger.warning("models fetch crashed for %s", self.name, exc_info=True)
            return {"harness": self.name, "ok": False, "error": "crashed"}
        if parsed.ok:
            self.models_fetched = list(parsed.models)
            self.models_fetched_at = Base.utc_now()
            db_session().flush()
        return {"harness": self.name, "ok": parsed.ok, "error": parsed.error}

    def update(self, **fields: object) -> None:
        # Callers pass column names only — the route's ``HarnessUpdate`` schema is
        # the trust boundary, so no field-name validation here.
        for field, value in fields.items():
            setattr(self, field, value)
        self.updated_at = Base.utc_now()
        db_session().flush()


class SettingsOverride(Base):
    __tablename__ = "settings_overrides"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)

    @classmethod
    def read(cls, key: str) -> Any | None:
        row = db_session().get(cls, key)
        return row.value if row else None

    @classmethod
    def write(cls, key: str, value: Any) -> None:
        session = db_session()
        row = session.get(cls, key)
        if value is None:
            if row:
                session.delete(row)
        elif row:
            row.value = value
        else:
            session.add(cls(key=key, value=value))
        session.flush()

    @classmethod
    def agent_model(cls, name: str, default: str) -> ResolvedModel:
        override = cls.read(f"agent_model:{name}")
        if override is not None:
            return ResolvedModel(override, "agent")
        # ``default`` is a harness name (claude/codex) → that harness's model,
        # or a pinned model string when it names no harness.
        harness = HarnessSettings.get(default)
        return ResolvedModel(harness.model if harness else default, "default")

    @classmethod
    def set_agent_model(cls, name: str, model: str | None) -> None:
        cls.write(f"agent_model:{name}", model)

    @classmethod
    def agent_effort(cls, name: str, declared: str | None, harness: str) -> ResolvedEffort:
        override = cls.read(f"agent_effort:{name}")
        if override is not None:
            return ResolvedEffort(override, "agent")
        if declared is not None:
            return ResolvedEffort(declared, "declared")
        return ResolvedEffort(HarnessSettings.require(harness).effort, "harness")

    @classmethod
    def set_agent_effort(cls, name: str, value: str | None) -> None:
        cls.write(f"agent_effort:{name}", value)

    @classmethod
    def agent_timeout(cls, name: str, declared: int | None, harness: str) -> ResolvedTimeout:
        override = cls.read(f"agent_timeout:{name}")
        if override is not None:
            return ResolvedTimeout(override, "agent")
        if declared is not None:
            return ResolvedTimeout(declared, "declared")
        return ResolvedTimeout(HarnessSettings.require(harness).timeout, "harness")

    @classmethod
    def set_agent_timeout(cls, name: str, value: int | None) -> None:
        cls.write(f"agent_timeout:{name}", value)

    @classmethod
    def workflow_setting(cls, kind: str, field: str, default: Any) -> Any:
        value = cls.read(f"workflow:{kind}:{field}")
        return default if value is None else value

    @classmethod
    def set_workflow_setting(cls, kind: str, field: str, value: Any) -> None:
        cls.write(f"workflow:{kind}:{field}", value)

    @classmethod
    def extension_setting(cls, extension: str, field: str, default: Any) -> Any:
        value = cls.read(f"extension:{extension}:{field}")
        return default if value is None else value

    @classmethod
    def set_extension_setting(cls, extension: str, field: str, value: Any) -> None:
        cls.write(f"extension:{extension}:{field}", value)
