from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, String, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.orm.attributes import flag_modified

from druks.accounts.models import Account
from druks.core.models import Uuid7Pk
from druks.database import db_session
from druks.models import Base
from druks.secrets.fields import EncryptedJsonField, EncryptedTextField
from druks.user_settings.models import UserSettings

from .exceptions import HarnessNotConnectedError, UnknownModelError

if TYPE_CHECKING:
    from .base import Harness


class ProviderSubscription(Base, Uuid7Pk):
    """One account's subscription at one provider."""

    __tablename__ = "provider_subscriptions"
    __table_args__ = (UniqueConstraint("provider", "account_id"),)

    provider: Mapped[str]
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    # The email the provider reported at connect. Snapshotted because an OAuth
    # payload carries no identity; a later connect refreshes it.
    provider_email: Mapped[str] = mapped_column(CITEXT)
    payload = EncryptedJsonField()
    expires_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(default=Base.utc_now, onupdate=Base.utc_now)

    @classmethod
    async def get(cls, subscription_id: str) -> "ProviderSubscription | None":
        return await db_session().get(cls, subscription_id)

    @classmethod
    async def lookup(
        cls, provider_id: str, account_id: str | None, *, subscription_id: str | None = None
    ) -> "ProviderSubscription":
        """The subscription a call runs with: the selected row, read fresh so a
        vanished one fails the call; else ``account_id``'s own. A miss raises."""
        from .providers import get_provider  # cycle: providers → models

        if subscription_id:
            if row := await cls.get(subscription_id):
                return row
            raise HarnessNotConnectedError(
                "the selected subscription was removed — reconnect it in Settings → Providers."
            )
        row = await cls.get_for_account(provider_id, account_id)
        if row:
            return row
        label = get_provider(provider_id).label
        raise HarnessNotConnectedError(
            f"connect your {label} subscription in Settings → Providers."
        )

    @classmethod
    async def get_for_account(
        cls, provider_id: str, account_id: str | None = None, *, fallback: bool = False
    ) -> "ProviderSubscription | None":
        """``fallback=True`` resolves the fallback account's subscription — what
        actor-less execution runs as."""
        if fallback:
            account_id = (await UserSettings.get()).fallback_account_id
        return await db_session().scalar(
            select(cls).where(cls.provider == provider_id, cls.account_id == account_id)
        )

    @classmethod
    async def list_all(cls) -> list["ProviderSubscription"]:
        return list(await db_session().scalars(select(cls).order_by(cls.provider, cls.id)))

    @classmethod
    async def list_for_account(cls, account_id: str) -> list["ProviderSubscription"]:
        stmt = select(cls).where(cls.account_id == account_id).order_by(cls.provider)
        return list(await db_session().scalars(stmt))

    @classmethod
    async def list_for_provider(cls, provider_id: str) -> list["ProviderSubscription"]:
        stmt = select(cls).where(cls.provider == provider_id).order_by(cls.id)
        return list(await db_session().scalars(stmt))

    @classmethod
    async def reload(cls, subscription_id: str) -> "ProviderSubscription | None":
        """Read one row past the identity map — what a refresher does after it
        wins the lock, so it never re-presents a token a peer already advanced."""
        return await db_session().scalar(
            select(cls).where(cls.id == subscription_id).execution_options(populate_existing=True)
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
    ) -> "ProviderSubscription":
        """Upsert ``account``'s subscription for this provider — update its
        existing row or create one."""
        session = db_session()
        row = await cls.get_for_account(provider, account.id)
        if not row:
            row = cls(provider=provider, account_id=account.id)
            session.add(row)
        row.payload = payload
        row.provider_email = provider_email
        row.expires_at = expires_at
        await session.flush()
        return row

    def get_harness(self) -> "type[Harness]":
        """The first registered harness that runs on this subscription; a miss raises."""
        from .registry import get_harnesses  # cycle: registry → base → models

        for harness in get_harnesses():
            if harness.accepts(self):
                return harness
        raise UnknownModelError(f"No installed harness runs a {self.provider} subscription.")

    @property
    def is_connected(self) -> bool:
        return not self.expires_at or self.expires_at > datetime.now(UTC)

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


class ProviderKey(Base):
    """The installation's API key at a provider; a second paste replaces it."""

    __tablename__ = "provider_keys"

    provider: Mapped[str] = mapped_column(String, primary_key=True)
    value = EncryptedTextField()
    updated_by_account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT")
    )
    updated_at: Mapped[datetime] = mapped_column(default=Base.utc_now, onupdate=Base.utc_now)
    updated_by: Mapped[Account] = relationship(lazy="joined")

    @classmethod
    async def get(cls, provider_id: str) -> "ProviderKey | None":
        return await db_session().get(cls, provider_id)

    @classmethod
    async def list_all(cls) -> list["ProviderKey"]:
        return list(await db_session().scalars(select(cls).order_by(cls.provider)))

    @classmethod
    async def create(cls, *, provider: str, key: str, account: Account) -> "ProviderKey":
        session = db_session()
        row = await cls.get(provider)
        if not row:
            row = cls(provider=provider)
            session.add(row)
        row.value = key
        row.updated_by = account
        await session.flush()
        # Reload so the row hands back a Secret, not the pasted str.
        await session.refresh(row)
        return row

    @property
    def key_tail(self) -> str:
        return self.value.decrypt()[-4:]

    async def delete(self) -> None:
        session = db_session()
        await session.delete(self)
        await session.flush()


class ProviderCatalog(Base):
    """The models a provider offers, ``{"id", "label"}`` each with ids
    namespaced ``provider/model``. Fetched over a subscription or the key."""

    __tablename__ = "provider_catalogs"

    provider: Mapped[str] = mapped_column(String, primary_key=True)
    models: Mapped[Any] = mapped_column(JSONB)
    fetched_at: Mapped[datetime] = mapped_column(default=Base.utc_now, onupdate=Base.utc_now)

    @classmethod
    async def list_all(cls) -> list["ProviderCatalog"]:
        return list(await db_session().scalars(select(cls).order_by(cls.provider)))

    @classmethod
    async def create(cls, provider: str, models: list[dict]) -> None:
        session = db_session()
        row = await session.get(cls, provider)
        if row:
            row.models = models
            row.fetched_at = Base.utc_now()
        else:
            session.add(cls(provider=provider, models=models))
        await session.flush()
