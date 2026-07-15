from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, select, text
from sqlalchemy.orm import Mapped, mapped_column

from druks.accounts.models import Account
from druks.core.models import Uuid7Pk
from druks.database import db_session
from druks.models import Base
from druks.secrets.fields import EncryptedJsonField


class HarnessLogin(Base, Uuid7Pk):
    __tablename__ = "harness_logins"
    __table_args__ = (
        UniqueConstraint("harness", "account_id"),
        Index(
            "harness_logins_provider_email_idx",
            "harness",
            "provider_email",
            unique=True,
            postgresql_where=text("provider_email IS NOT NULL"),
        ),
        # One designated default seat per harness — the row execution and the
        # settings card resolve while runs aren't seat-aware.
        Index(
            "harness_logins_default_idx",
            "harness",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )

    harness: Mapped[str]
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    # The identity the provider reported for this seat, normalized lowercase.
    # Null on a row migrated without one — filled in on reconnect.
    provider_email: Mapped[str | None]
    kind: Mapped[str] = mapped_column(String, default="subscription")
    payload = EncryptedJsonField()
    expires_at: Mapped[datetime | None]
    is_default: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(default=Base.utc_now, onupdate=Base.utc_now)

    @classmethod
    def get(cls, login_id: str) -> "HarnessLogin | None":
        return db_session().get(cls, login_id)

    @classmethod
    def get_default(cls, harness: str) -> "HarnessLogin | None":
        return db_session().scalar(select(cls).where(cls.harness == harness, cls.is_default))

    @classmethod
    def get_for_account(cls, harness: str, account_id: str) -> "HarnessLogin | None":
        return db_session().scalar(
            select(cls).where(cls.harness == harness, cls.account_id == account_id)
        )

    @classmethod
    def get_by_provider_email(cls, harness: str, provider_email: str) -> "HarnessLogin | None":
        return db_session().scalar(
            select(cls).where(cls.harness == harness, cls.provider_email == provider_email)
        )

    @classmethod
    def list_all(cls) -> list["HarnessLogin"]:
        return list(db_session().scalars(select(cls).order_by(cls.harness, cls.id)))

    @classmethod
    def reload(cls, login_id: str) -> "HarnessLogin | None":
        """Fresh-from-DB read of one row, past the identity map's cached state
        — the post-lock re-read that keeps a refresher from re-presenting a
        refresh token a concurrent winner already advanced."""
        return db_session().scalar(
            select(cls).where(cls.id == login_id).execution_options(populate_existing=True)
        )

    @classmethod
    def connect(
        cls,
        *,
        harness: str,
        payload: dict,
        expires_at: datetime | None,
        provider_email: str,
    ) -> "HarnessLogin":
        """Upsert the seat a finished connect flow authenticated: the row
        already carrying this provider email, else the account's row for this
        harness — creating the account from the provider email when it's new.
        A seat connected while the harness has no default becomes the default;
        promotion only ever happens through a connect, never a disconnect."""
        email = Account.normalize_email(provider_email)
        session = db_session()
        row = cls.get_by_provider_email(harness, email)
        if not row:
            account = Account.get_or_create(email)
            row = cls.get_for_account(harness, account.id)
            if not row:
                row = cls(harness=harness, account_id=account.id)
                session.add(row)
        row.payload = payload
        row.provider_email = email
        row.expires_at = expires_at
        if not cls.get_default(harness):
            row.is_default = True
        session.flush()
        return row

    def update_payload(self, payload: dict, *, expires_at: datetime | None) -> None:
        # Whole-value reassignment is the write path: the encrypted column
        # re-encrypts what it's handed, and an in-place edit of a nested block
        # would not mark the column dirty.
        self.payload = payload
        self.expires_at = expires_at
        db_session().flush()

    def delete(self) -> None:
        session = db_session()
        session.delete(self)
        session.flush()
