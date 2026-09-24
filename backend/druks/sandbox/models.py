import hashlib
import hmac
from datetime import datetime, timedelta
from secrets import token_urlsafe
from typing import TYPE_CHECKING

from drukbox_sdk import Issuer
from sqlalchemy import ForeignKey, LargeBinary, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship, selectinload

from druks.core.models import Uuid7Pk
from druks.database import get_session
from druks.mcp.constants import BEARER_HEADER, BEARER_PREFIX
from druks.models import Base
from druks.secrets.enums import SecretKind
from druks.secrets.models import VaultSecret
from druks.settings import load_settings

from .constants import SANDBOX_HOST_LEASE_SECONDS
from .exceptions import IdentityDenied

if TYPE_CHECKING:
    from druks.durable.models import Run

# The lifetime the exchange gives an answer without expires_at. A
# subscription answer always carries one. Nothing polls on it.
_ISSUER_REFRESH = "1h"
# A pasted value never expires, so the exchange fetches it again on this
# cadence: a new paste reaches a running box within it.
_STATIC_REFRESH = "5m"


class SecretRef(Base):
    """One secret a box holds as a placeholder: the vault row the issuer
    answers from, under the box's name for it."""

    __tablename__ = "sandbox_secret_refs"

    identity_id: Mapped[str] = mapped_column(
        ForeignKey("sandbox_identities.id", ondelete="CASCADE"), primary_key=True
    )
    # The Drukbox catalog name: the key of the box's secret and its variable.
    name: Mapped[str] = mapped_column(primary_key=True)
    secret_id: Mapped[str] = mapped_column(ForeignKey("vault.id", ondelete="CASCADE"))
    # What the token is for: the repo. Empty for a subscription.
    resource: Mapped[str] = mapped_column(default="")
    # The host the box's placeholder is swapped at, for a custom entry such
    # as an MCP server. Empty for a Drukbox catalog entry.
    host: Mapped[str] = mapped_column(default="")

    identity: Mapped["SandboxIdentity"] = relationship(back_populates="secret_refs", lazy="raise")
    secret: Mapped[VaultSecret] = relationship(lazy="selectin")

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.name, self.secret_id, self.resource or "", self.host or "")


class SandboxIdentity(Base, Uuid7Pk):
    """One account's box at the issuer, with an optional run.

    The row stores the bearer hash. Only the box's issuer headers hold the bearer.
    """

    __tablename__ = "sandbox_identities"

    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("durable_runs.id", ondelete="CASCADE"), default=None
    )
    run: Mapped["Run | None"] = relationship(lazy="raise")
    scoped_to: Mapped[str]
    # Bound once Drukbox returns the box. One box holds one identity.
    host_id: Mapped[str | None] = mapped_column(unique=True)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    secret_refs: Mapped[list[SecretRef]] = relationship(
        back_populates="identity", cascade="all, delete-orphan", lazy="selectin"
    )

    @classmethod
    async def create(
        cls,
        session: AsyncSession,
        *,
        account_id: str,
        run_id: str | None,
        scoped_to: str,
        secret_refs: list[SecretRef],
    ) -> tuple["SandboxIdentity", dict[str, Issuer]]:
        """The committed identity and the issuer entries for its box. Committed
        before the box exists: Drukbox fetches an issuer during provisioning."""
        bearer = token_urlsafe(32)
        now = Base.utc_now()
        identity = cls(
            account_id=account_id,
            run_id=run_id,
            scoped_to=scoped_to,
            secret_refs=[
                SecretRef(
                    name=ref.name,
                    secret_id=ref.secret_id,
                    resource=ref.resource or "",
                    host=ref.host or "",
                )
                for ref in secret_refs
            ],
            token_hash=hashlib.sha256(bearer.encode()).digest(),
            created_at=now,
            # The box lease bounds the identity: a box never outlives it.
            expires_at=now + timedelta(seconds=SANDBOX_HOST_LEASE_SECONDS),
        )
        session.add(identity)
        await session.commit()
        issuer_url = load_settings().sandbox.issuer_url.rstrip("/")
        entries = {}
        for ref in secret_refs:
            secret = await session.get(VaultSecret, ref.secret_id)
            # A custom entry names its host, variable, and header; a catalog
            # entry leaves those to Drukbox.
            fields = {}
            if ref.host:
                # A header row stores the verbatim header value; a headerless
                # row is a raw token the header frames at egress.
                fields = {
                    "host": ref.host,
                    "auth_variable": ref.name.upper(),
                    "auth_header": secret.header or BEARER_HEADER,
                    "auth_prefix": "" if secret.header else BEARER_PREFIX,
                }
            entries[ref.name] = Issuer(
                url=f"{issuer_url}/api/secrets/{identity.id}/{ref.name}",
                headers={"Authorization": f"Bearer {bearer}"},
                refresh=_STATIC_REFRESH if secret.kind == SecretKind.STATIC else _ISSUER_REFRESH,
                **fields,
            )
        return identity, entries

    @classmethod
    async def lookup(
        cls,
        session: AsyncSession,
        *,
        account_id: str,
        run_id: str | None,
        scoped_to: str,
        secret_refs: list[SecretRef] | None = None,
    ) -> "SandboxIdentity | None":
        """Find a live box for this account, run, and scope.

        Omit secret_refs to accept the box's current secrets.
        """
        rows = await session.scalars(
            select(cls)
            .options(selectinload(cls.run))
            .where(
                cls.account_id == account_id,
                cls.run_id == run_id,
                cls.scoped_to == scoped_to,
                cls.host_id.is_not(None),
                cls.revoked_at.is_(None),
            )
            .order_by(cls.id.desc())
        )
        wanted = {ref.key for ref in secret_refs} if secret_refs is not None else None
        return next(
            (
                row
                for row in rows
                if row.is_live
                and (not row.run or row.run.is_active)
                and (wanted is None or {ref.key for ref in row.secret_refs} == wanted)
            ),
            None,
        )

    @classmethod
    async def authenticate(
        cls, session: AsyncSession, identity_id: str, bearer: str, name: str
    ) -> "SandboxIdentity":
        """Authenticate a live identity and its named secret.

        A run-bound identity also requires an active run.
        """
        identity = await session.scalar(
            select(cls)
            .options(selectinload(cls.run), selectinload(cls.secret_refs))
            .where(cls.id == identity_id)
            .execution_options(populate_existing=True)
        )
        if (
            identity
            and hmac.compare_digest(hashlib.sha256(bearer.encode()).digest(), identity.token_hash)
            and identity.is_live
            and (not identity.run or identity.run.is_active)
        ):
            identity.get_secret_ref(name)
            return identity
        raise IdentityDenied(f"no live identity {identity_id} for a fetch of {name}")

    def get_secret_ref(self, name: str) -> SecretRef:
        """The secret the box holds under a Drukbox name, or IdentityDenied."""
        for ref in self.secret_refs:
            if ref.name == name:
                return ref
        raise IdentityDenied(f"identity {self.id} holds no secret {name}")

    @property
    def is_live(self) -> bool:
        return not self.revoked_at and self.expires_at > Base.utc_now()

    async def bind(self, host_id: str) -> None:
        self.host_id = host_id
        await self.session.commit()

    async def revoke(self) -> None:
        # A second revoke keeps the first stamp.
        self.revoked_at = self.revoked_at or Base.utc_now()
        await self.session.commit()

    @classmethod
    async def list_orphans(cls, session: AsyncSession) -> list["SandboxIdentity"]:
        """The identities of boxes whose run ended: bound, inside their lease, not revoked."""
        rows = await session.scalars(
            select(cls)
            .options(selectinload(cls.run))
            .where(
                cls.run_id.is_not(None),
                cls.host_id.is_not(None),
                cls.revoked_at.is_(None),
                cls.expires_at > Base.utc_now(),
            )
        )
        return [identity for identity in rows if identity.run and not identity.run.is_active]

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
    async def list_for_secret(
        cls, session: AsyncSession, secret_id: str
    ) -> list["SandboxIdentity"]:
        """The live identities with a bound box that hold a ref to the secret."""
        rows = await session.scalars(
            select(cls)
            .join(cls.secret_refs)
            .where(
                SecretRef.secret_id == secret_id,
                cls.host_id.is_not(None),
                cls.revoked_at.is_(None),
            )
            .distinct()
        )
        return [row for row in rows if row.is_live]
