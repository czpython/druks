from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, String, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.orm.attributes import flag_modified

from druks.accounts.models import Account
from druks.core.models import Uuid7Pk
from druks.database import db_session
from druks.models import Base
from druks.secrets.fields import EncryptedJsonField
from druks.user_settings.models import UserSettings

from .exceptions import HarnessNotConnectedError, UnknownModelError

if TYPE_CHECKING:
    from .base import Harness


class ProviderLogin(Base, Uuid7Pk):
    """One account's grant to one provider: an API key or an OAuth token set.
    Every harness that drives the provider runs on this row."""

    __tablename__ = "provider_logins"
    __table_args__ = (UniqueConstraint("provider", "account_id"),)

    provider: Mapped[str]
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    # The email the provider reported at connect. Snapshotted because an OAuth
    # payload carries no identity; a later connect refreshes it.
    provider_email: Mapped[str] = mapped_column(CITEXT)
    # "oauth" | "api_key"
    kind: Mapped[str] = mapped_column(String)
    payload = EncryptedJsonField()
    expires_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(default=Base.utc_now, onupdate=Base.utc_now)

    @classmethod
    async def get(cls, login_id: str) -> "ProviderLogin | None":
        return await db_session().get(cls, login_id)

    @classmethod
    async def lookup(
        cls, provider_id: str, account_id: str | None, *, login_id: str | None = None
    ) -> "ProviderLogin":
        """The login a call runs with: the selected row, read fresh so a vanished
        one fails the call; else the account's own; else the fallback account's,
        which carries unmatched work so automation keeps moving."""
        if login_id:
            if row := await cls.get(login_id):
                return row
            raise HarnessNotConnectedError(
                "the selected login was removed — reconnect it in Settings → Providers."
            )
        if account_id:
            own = await cls.get_for_account(provider_id, account_id)
            if own:
                return own
        fallback = await cls.get_for_account(provider_id, fallback=True)
        if fallback:
            return fallback
        raise HarnessNotConnectedError(
            f"{provider_id} is not connected for the fallback account — connect it in "
            "Settings → Providers."
        )

    @classmethod
    async def get_for_account(
        cls, provider_id: str, account_id: str | None = None, *, fallback: bool = False
    ) -> "ProviderLogin | None":
        """``fallback=True`` resolves the fallback account's login — what
        actor-less execution runs as."""
        if fallback:
            account_id = (await UserSettings.get()).fallback_account_id
        return await db_session().scalar(
            select(cls).where(cls.provider == provider_id, cls.account_id == account_id)
        )

    @classmethod
    async def list_all(cls) -> list["ProviderLogin"]:
        return list(await db_session().scalars(select(cls).order_by(cls.provider, cls.id)))

    @classmethod
    async def list_for_account(cls, account_id: str) -> list["ProviderLogin"]:
        stmt = select(cls).where(cls.account_id == account_id).order_by(cls.provider)
        return list(await db_session().scalars(stmt))

    @classmethod
    async def list_for_provider(cls, provider_id: str) -> list["ProviderLogin"]:
        stmt = select(cls).where(cls.provider == provider_id).order_by(cls.id)
        return list(await db_session().scalars(stmt))

    @classmethod
    async def reload(cls, login_id: str) -> "ProviderLogin | None":
        """Read one row past the identity map — what a refresher does after it
        wins the lock, so it never re-presents a token a peer already advanced."""
        return await db_session().scalar(
            select(cls).where(cls.id == login_id).execution_options(populate_existing=True)
        )

    @classmethod
    async def connect(
        cls,
        *,
        provider: str,
        account: Account,
        payload: dict,
        expires_at: datetime | None,
        provider_email: str,
        kind: str,
    ) -> "ProviderLogin":
        """Upsert ``account``'s login for this provider — update its
        existing row or create one."""
        session = db_session()
        row = await cls.get_for_account(provider, account.id)
        if not row:
            row = cls(provider=provider, account_id=account.id)
            session.add(row)
        row.kind = kind
        row.payload = payload
        row.provider_email = provider_email
        row.expires_at = expires_at
        await session.flush()
        return row

    def get_harness(self) -> "type[Harness]":
        """The first registered harness that runs on this login; a miss raises."""
        from .registry import get_harnesses  # cycle: registry → base → models

        for harness in get_harnesses():
            if harness.accepts(self):
                return harness
        raise UnknownModelError(
            f"No installed harness runs {self.provider} on a {self.kind} login."
        )

    @property
    def is_connected(self) -> bool:
        return not self.expires_at or self.expires_at > datetime.now(UTC)

    @property
    def supports_refresh(self) -> bool:
        return self.kind == "oauth"

    @property
    def is_metered(self) -> bool:
        return self.kind == "oauth"

    async def update_payload(self, payload: dict, *, expires_at: datetime | None) -> None:
        # A shallow copy aliases the live nested blocks, so old and new compare
        # equal at flush and the UPDATE is skipped; force the write.
        self.payload = payload
        flag_modified(self, "payload")
        self.expires_at = expires_at
        await db_session().flush()

    async def delete(self) -> None:
        session = db_session()
        await session.delete(self)
        await session.flush()


class ProviderCatalog(Base):
    """The models a provider offers, ``{"id", "label"}`` each with ids
    namespaced ``provider/model``. Fetched over one of its logins."""

    __tablename__ = "provider_catalogs"

    provider: Mapped[str] = mapped_column(String, primary_key=True)
    models: Mapped[Any] = mapped_column(JSONB)
    fetched_at: Mapped[datetime] = mapped_column(default=Base.utc_now, onupdate=Base.utc_now)

    @classmethod
    async def list_all(cls) -> list["ProviderCatalog"]:
        return list(await db_session().scalars(select(cls).order_by(cls.provider)))

    @classmethod
    async def store(cls, provider: str, models: list[dict]) -> None:
        session = db_session()
        row = await session.get(cls, provider)
        if row:
            row.models = models
            row.fetched_at = Base.utc_now()
        else:
            session.add(cls(provider=provider, models=models))
        await session.flush()
