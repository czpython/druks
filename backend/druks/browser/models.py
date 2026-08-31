from datetime import datetime

from sqlalchemy import CheckConstraint, String, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Mapped, mapped_column

from druks.browser.constants import (
    BROWSER_SESSION_NAME_MAX_LENGTH,
    SITE_MAX_LENGTH,
)
from druks.browser.enums import BrowserSessionPayloadFormat, BrowserSessionStatus
from druks.core.models import Uuid7Pk
from druks.database import db_session
from druks.models import Base
from druks.secrets.fields import EncryptedBytesField, SecretBytes


class StoredBrowserSession(Base, Uuid7Pk):
    __tablename__ = "browser_sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('needs_login', 'ready', 'stale', 'anonymous')",
            name="browser_sessions_status_check",
        ),
        CheckConstraint(
            "payload_format IN ('storage_state', 'profile_dir')",
            name="browser_sessions_payload_format_check",
        ),
    )

    name: Mapped[str] = mapped_column(String(BROWSER_SESSION_NAME_MAX_LENGTH), unique=True)
    status: Mapped[str] = mapped_column(String(16), default=BrowserSessionStatus.NEEDS_LOGIN.value)
    payload_format: Mapped[str] = mapped_column(String(16))
    payload: Mapped[SecretBytes] = EncryptedBytesField(default=b"")
    site: Mapped[str] = mapped_column(String(SITE_MAX_LENGTH))
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(default=None)
    last_used_at: Mapped[datetime | None] = mapped_column(default=None)

    @classmethod
    async def get_or_create(
        cls,
        *,
        name: str,
        payload_format: BrowserSessionPayloadFormat,
        site: str,
        status: BrowserSessionStatus = BrowserSessionStatus.NEEDS_LOGIN,
    ):
        """Concurrency-safe lookup-or-create: two first actions racing on the
        same session both INSERT with ON CONFLICT DO NOTHING, then converge on
        the one row through the name lookup."""
        browser_session = await cls.get_for_name(name)
        if browser_session:
            return browser_session
        session = db_session()
        await session.execute(
            insert(cls)
            .values(name=name, payload_format=payload_format.value, site=site, status=status.value)
            .on_conflict_do_nothing(index_elements=["name"])
        )
        return (await session.scalars(select(cls).where(cls.name == name))).one()

    @classmethod
    async def list_all(cls):
        return list(await db_session().scalars(select(cls).order_by(cls.name)))

    @classmethod
    async def get_for_name(cls, name: str):
        return await db_session().scalar(select(cls).where(cls.name == name))

    async def mark_stale(self) -> None:
        self.status = BrowserSessionStatus.STALE.value
        await db_session().flush()

    async def mark_used(self) -> None:
        self.last_used_at = Base.utc_now()
        await db_session().flush()

    async def store_payload(self, payload: bytes) -> None:
        self.payload = payload  # type: ignore[assignment] — the column takes plaintext in, hands SecretBytes back
        self.status = BrowserSessionStatus.READY.value
        self.last_refreshed_at = Base.utc_now()
        await db_session().flush()
        # Assignment holds the plaintext; a read must always hand back the
        # encrypted column's SecretBytes, so reload the column now — an expired
        # attribute can't lazy-load under the async session.
        await db_session().refresh(self, ["payload"])

    async def delete(self) -> None:
        await db_session().delete(self)
        await db_session().flush()
