from datetime import datetime

from sqlalchemy import CheckConstraint, String, select
from sqlalchemy.orm import Mapped, mapped_column

from druks.browser.constants import (
    BROWSER_SESSION_NAME_MAX_LENGTH,
    SITE_MAX_LENGTH,
)
from druks.browser.enums import BrowserSessionPayloadFormat, BrowserSessionStatus
from druks.core.models import Uuid7Pk
from druks.database import db_session
from druks.models import Base
from druks.secrets.fields import EncryptedBytesField


class BrowserSession(Base, Uuid7Pk):
    __tablename__ = "browser_sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('needs_login', 'ready', 'stale')",
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
    payload = EncryptedBytesField(default=b"")
    site: Mapped[str] = mapped_column(String(SITE_MAX_LENGTH))
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(default=None)
    last_used_at: Mapped[datetime | None] = mapped_column(default=None)

    @classmethod
    def create(
        cls,
        *,
        name: str,
        payload_format: BrowserSessionPayloadFormat,
        site: str,
    ):
        browser_session = cls(
            name=name,
            payload_format=payload_format.value,
            site=site,
        )
        db_session().add(browser_session)
        db_session().flush()
        return browser_session

    @classmethod
    def get_for_id(cls, session_id: str):
        return db_session().get(cls, session_id)

    @classmethod
    def list_all(cls):
        return list(db_session().scalars(select(cls).order_by(cls.name)))

    def rename(self, name: str) -> None:
        self.name = name
        db_session().flush()

    def store_payload(self, payload: bytes) -> None:
        self.payload = payload
        self.status = BrowserSessionStatus.READY.value
        self.last_refreshed_at = Base.utc_now()
        db_session().flush()

    def delete(self) -> None:
        db_session().delete(self)
        db_session().flush()
