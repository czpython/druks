from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, select, text
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.orm.attributes import flag_modified

from druks.accounts.models import Account
from druks.core.models import Uuid7Pk
from druks.database import db_session
from druks.models import Base
from druks.secrets.fields import EncryptedJsonField


class HarnessConnection(Base, Uuid7Pk):
    __tablename__ = "harness_logins"
    __table_args__ = (
        UniqueConstraint("harness", "account_id"),
        # One designated default connection per harness — the row execution and
        # the settings card resolve while runs aren't account-aware.
        Index(
            "harness_logins_default_idx",
            "harness",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )

    harness: Mapped[str]
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    # The email the provider reported at the token exchange (citext — matched
    # case-insensitively). Cached because Claude states it once and its stored
    # payload carries no identity at all — without this the connection is
    # anonymous forever. A connect-time snapshot, not an identifier: it goes
    # stale if the account is renamed upstream, and refreshes on the next
    # connect. Null on a row migrated without one.
    provider_email: Mapped[str | None] = mapped_column(CITEXT)
    kind: Mapped[str] = mapped_column(String, default="subscription")
    payload = EncryptedJsonField()
    expires_at: Mapped[datetime | None]
    is_default: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(default=Base.utc_now, onupdate=Base.utc_now)

    @classmethod
    def get(cls, login_id: str) -> "HarnessConnection | None":
        return db_session().get(cls, login_id)

    @classmethod
    def get_default(cls, harness: str) -> "HarnessConnection | None":
        return db_session().scalar(select(cls).where(cls.harness == harness, cls.is_default))

    @classmethod
    def get_for_account(cls, harness: str, account_id: str) -> "HarnessConnection | None":
        return db_session().scalar(
            select(cls).where(cls.harness == harness, cls.account_id == account_id)
        )

    @classmethod
    def list_all(cls) -> list["HarnessConnection"]:
        return list(db_session().scalars(select(cls).order_by(cls.harness, cls.id)))

    @classmethod
    def reload(cls, login_id: str) -> "HarnessConnection | None":
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
    ) -> "HarnessConnection":
        """Upsert the account's connection for this harness, creating the
        account from the provider's email when it's new. A connection made
        while the harness has no default becomes the default; promotion only
        ever happens through a connect, never a disconnect."""
        account = Account.get_or_create(provider_email)
        session = db_session()
        row = cls.get_for_account(harness, account.id)
        if not row:
            row = cls(harness=harness, account_id=account.id)
            session.add(row)
        row.payload = payload
        row.provider_email = provider_email
        row.expires_at = expires_at
        if not cls.get_default(harness):
            row.is_default = True
        session.flush()
        return row

    def update_payload(self, payload: dict, *, expires_at: datetime | None) -> None:
        # Whole-value reassignment is the write path: the encrypted column
        # re-encrypts what it's handed. The caller's dict may alias the live
        # mapping's nested blocks (a dict() copy is shallow), making old and
        # new compare content-equal at flush and the UPDATE get skipped —
        # force the write; this is a secrets store, not somewhere to lose a
        # token.
        self.payload = payload
        flag_modified(self, "payload")
        self.expires_at = expires_at
        db_session().flush()

    def delete(self) -> None:
        session = db_session()
        session.delete(self)
        session.flush()
