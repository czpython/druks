import hashlib
import hmac
from datetime import datetime, timedelta
from secrets import token_urlsafe
from typing import TYPE_CHECKING

from drukbox_sdk import Issuer
from sqlalchemy import CheckConstraint, ForeignKey, LargeBinary, select, update
from sqlalchemy.orm import Mapped, mapped_column, relationship, selectinload

from druks.core.models import Uuid7Pk
from druks.database import db_session, get_session
from druks.models import Base
from druks.settings import load_settings

from .constants import SANDBOX_HOST_LEASE_SECONDS
from .exceptions import IdentityDenied

if TYPE_CHECKING:
    from druks.durable.models import Run

# The lifetime the exchange gives an answer without expires_at. A
# subscription answer always carries one. Nothing polls on it.
_ISSUER_REFRESH = "1h"


class SandboxSecret(Base):
    """One secret a box holds as a placeholder, and where the issuer gets its
    value: an identity of the appliance at a service, or a harness login."""

    __tablename__ = "sandbox_secrets"

    identity_id: Mapped[str] = mapped_column(
        ForeignKey("sandbox_identities.id", ondelete="CASCADE"), primary_key=True
    )
    # The Drukbox catalog name: the key of the box's secret and its variable.
    name: Mapped[str] = mapped_column(primary_key=True)
    # The appliance identity that issues, by service slug.
    service: Mapped[str | None] = mapped_column(
        ForeignKey("service_identities.service", ondelete="CASCADE")
    )
    # The harness login that issues.
    subscription_id: Mapped[str | None] = mapped_column(
        ForeignKey("provider_subscriptions.id", ondelete="CASCADE")
    )
    # What the token is for: the repo. Empty for a subscription.
    resource: Mapped[str] = mapped_column(default="")

    __table_args__ = (
        CheckConstraint("(service IS NULL) <> (subscription_id IS NULL)", name="one_source"),
    )

    identity: Mapped["SandboxIdentity"] = relationship(back_populates="secrets")

    @property
    def key(self) -> tuple[str, str | None, str | None, str]:
        return (self.name, self.service, self.subscription_id, self.resource or "")


class SandboxIdentity(Base, Uuid7Pk):
    """One box at the issuer. The bearer rides only in the box's issuer
    headers. The row keeps its hash, the run, and the secrets the box holds."""

    __tablename__ = "sandbox_identities"

    run_id: Mapped[str] = mapped_column(ForeignKey("durable_runs.id", ondelete="CASCADE"))
    run: Mapped["Run"] = relationship()
    # What the box serves in its run: ``workflow`` for the warm box, the agent
    # id for an ephemeral one. A replay finds the box through it.
    scoped_to: Mapped[str]
    # Bound once Drukbox returns the box. One box holds one identity.
    host_id: Mapped[str | None] = mapped_column(unique=True)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    secrets: Mapped[list[SandboxSecret]] = relationship(
        back_populates="identity", cascade="all, delete-orphan", lazy="selectin"
    )

    @classmethod
    async def create(
        cls, *, run_id: str, scoped_to: str, secrets: list[SandboxSecret]
    ) -> tuple["SandboxIdentity", dict[str, Issuer]]:
        """The committed identity and the issuer entries for its box. Committed
        before the box exists: Drukbox fetches an issuer during provisioning."""
        bearer = token_urlsafe(32)
        now = Base.utc_now()
        identity = cls(
            run_id=run_id,
            scoped_to=scoped_to,
            # Own rows: the caller's values stay values.
            secrets=[
                SandboxSecret(
                    name=secret.name,
                    service=secret.service,
                    subscription_id=secret.subscription_id,
                    resource=secret.resource or "",
                )
                for secret in secrets
            ],
            token_hash=hashlib.sha256(bearer.encode()).digest(),
            created_at=now,
            # The box lease bounds the identity: a box never outlives it.
            expires_at=now + timedelta(seconds=SANDBOX_HOST_LEASE_SECONDS),
        )
        session = db_session()
        session.add(identity)
        await session.commit()
        issuer_url = load_settings().sandbox.issuer_url.rstrip("/")
        entries = {
            secret.name: Issuer(
                url=f"{issuer_url}/api/secrets/{identity.id}/{secret.name}",
                headers={"Authorization": f"Bearer {bearer}"},
                refresh=_ISSUER_REFRESH,
            )
            for secret in secrets
        }
        return identity, entries

    @classmethod
    async def lookup(
        cls, run_id: str, scoped_to: str, secrets: list[SandboxSecret]
    ) -> "SandboxIdentity | None":
        """The live identity bound to the box scoped to a workflow or an agent,
        with these secrets. A replay finds the box a crashed process left."""
        rows = await db_session().scalars(
            select(cls)
            .where(
                cls.run_id == run_id,
                cls.scoped_to == scoped_to,
                cls.host_id.is_not(None),
                cls.revoked_at.is_(None),
            )
            .order_by(cls.id.desc())
        )
        wanted = {secret.key for secret in secrets}
        return next(
            (row for row in rows if row.is_live and {s.key for s in row.secrets} == wanted),
            None,
        )

    @classmethod
    async def authenticate(cls, identity_id: str, bearer: str, name: str) -> "SandboxIdentity":
        """The live identity of an active run that a fetch presents, holding the
        secret it names, read fresh. Else IdentityDenied."""
        identity = await db_session().scalar(
            select(cls)
            .options(selectinload(cls.run), selectinload(cls.secrets))
            .where(cls.id == identity_id)
            .execution_options(populate_existing=True)
        )
        if (
            identity
            and hmac.compare_digest(hashlib.sha256(bearer.encode()).digest(), identity.token_hash)
            and identity.is_live
            and identity.run.is_active
        ):
            identity.get_secret(name)
            return identity
        raise IdentityDenied(f"no live identity {identity_id} for a fetch of {name}")

    def get_secret(self, name: str) -> SandboxSecret:
        """The secret the box holds under a Drukbox name, or IdentityDenied."""
        for secret in self.secrets:
            if secret.name == name:
                return secret
        raise IdentityDenied(f"identity {self.id} holds no secret {name}")

    @property
    def is_live(self) -> bool:
        return not self.revoked_at and self.expires_at > Base.utc_now()

    async def bind(self, host_id: str) -> None:
        self.host_id = host_id
        await db_session().commit()

    async def revoke(self) -> None:
        self.revoked_at = Base.utc_now()
        await db_session().commit()

    @classmethod
    async def revoke_for_host(cls, engine, host_id: str) -> None:
        # Own transaction: a box release can run outside a step session.
        async with get_session(engine) as session:
            await session.execute(
                update(cls)
                .where(cls.host_id == host_id, cls.revoked_at.is_(None))
                .values(revoked_at=Base.utc_now())
            )
            await session.commit()

    @classmethod
    async def list_subscription_identities(cls, subscription_id: str) -> list["SandboxIdentity"]:
        """The live identities with a bound box that hold a secret of the subscription."""
        rows = await db_session().scalars(
            select(cls)
            .join(cls.secrets)
            .where(
                SandboxSecret.subscription_id == subscription_id,
                cls.host_id.is_not(None),
                cls.revoked_at.is_(None),
            )
            .distinct()
        )
        return [row for row in rows if row.is_live]
