import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from drukbox_sdk import Issuer
from sqlalchemy import ForeignKey, LargeBinary, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship, selectinload

from druks.core.models import Uuid7Pk
from druks.database import db_session, get_session
from druks.models import Base
from druks.settings import load_settings

from .constants import SANDBOX_HOST_LEASE_SECONDS
from .exceptions import GrantDenied

if TYPE_CHECKING:
    from druks.durable.models import Run

# The lifetime the exchange gives an answer without expires_at. A
# subscription answer always carries one. Nothing polls on it.
_ISSUER_REFRESH = "1h"


class SandboxGrant(Base, Uuid7Pk):
    """One box's credential at the issuer. The bearer rides only in the box's
    issuer headers. The row keeps its hash, the run, and the services the box
    can fetch."""

    __tablename__ = "sandbox_grants"

    run_id: Mapped[str] = mapped_column(ForeignKey("durable_runs.id", ondelete="CASCADE"))
    run: Mapped["Run"] = relationship()
    # What the box serves in its run: ``workflow`` for the warm box, the agent
    # id for an ephemeral one. A replay finds the box through it.
    scoped_to: Mapped[str]
    # Bound once Drukbox returns the box. One box holds one grant.
    host_id: Mapped[str | None] = mapped_column(unique=True)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary)
    # Service name → the subscription the box fetches.
    services: Mapped[dict[str, str]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]

    @classmethod
    async def create(
        cls, *, run_id: str, scoped_to: str, services: dict[str, str]
    ) -> tuple["SandboxGrant", dict[str, Issuer]]:
        """The committed grant and the issuer entries for its box. Committed
        before the box exists: Drukbox fetches an issuer during provisioning."""
        bearer = secrets.token_urlsafe(32)
        now = Base.utc_now()
        grant = cls(
            run_id=run_id,
            scoped_to=scoped_to,
            services=services,
            token_hash=hashlib.sha256(bearer.encode()).digest(),
            created_at=now,
            # The box lease bounds the grant: a box never outlives it.
            expires_at=now + timedelta(seconds=SANDBOX_HOST_LEASE_SECONDS),
        )
        session = db_session()
        session.add(grant)
        await session.commit()
        issuer_url = load_settings().sandbox.issuer_url.rstrip("/")
        entries = {
            service: Issuer(
                url=f"{issuer_url}/api/secrets/{grant.id}/{service}",
                headers={"Authorization": f"Bearer {bearer}"},
                refresh=_ISSUER_REFRESH,
            )
            for service in services
        }
        return grant, entries

    @classmethod
    async def lookup(
        cls, run_id: str, scoped_to: str, services: dict[str, str]
    ) -> "SandboxGrant | None":
        """The live grant bound to the box scoped to a workflow or an agent, for
        these services. A replay finds the box a crashed process left behind."""
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
        return next((grant for grant in rows if grant.is_live and grant.services == services), None)

    @classmethod
    async def authenticate(cls, grant_id: str, bearer: str, service: str) -> "SandboxGrant":
        """The live grant of an active run that a fetch presents, read fresh,
        or GrantDenied."""
        grant = await db_session().scalar(
            select(cls)
            .options(selectinload(cls.run))
            .where(cls.id == grant_id)
            .execution_options(populate_existing=True)
        )
        if (
            grant
            and hmac.compare_digest(hashlib.sha256(bearer.encode()).digest(), grant.token_hash)
            and service in grant.services
            and grant.is_live
            and grant.run.is_active
        ):
            return grant
        raise GrantDenied(f"no live grant {grant_id} for service {service}")

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
    async def list_live_for_subscription(cls, subscription_id: str) -> list["SandboxGrant"]:
        """The live grants with a bound box that name the subscription."""
        rows = await db_session().scalars(
            select(cls).where(cls.host_id.is_not(None), cls.revoked_at.is_(None))
        )
        return [
            grant for grant in rows if grant.is_live and subscription_id in grant.services.values()
        ]
