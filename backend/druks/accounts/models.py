import base64
import hashlib
import hmac
import secrets
from datetime import datetime

from sqlalchemy import ForeignKey, Index, LargeBinary, String, select
from sqlalchemy.dialects.postgresql import CITEXT, insert
from sqlalchemy.orm import Mapped, mapped_column, relationship

from druks.accounts.constants import (
    PAT_LAST_USED_RESOLUTION,
    PAT_LIFETIME,
    PAT_NAME_LENGTH,
    PAT_PREFIX_ALPHABET,
    PAT_PREFIX_LENGTH,
    PAT_SECRET_BYTES,
    PAT_TOKEN_TAG,
    SYSTEM_ACCOUNT_ID,
)
from druks.accounts.exceptions import InvalidPatError
from druks.core.models import Uuid7Pk
from druks.database import db_session
from druks.models import Base


class Account(Base, Uuid7Pk):
    __tablename__ = "accounts"

    # citext: the column compares and enforces uniqueness case-insensitively,
    # so a lookup or a duplicate check needs no normalization — the username is
    # stored as the provider gave it and matched regardless of case. Usually a
    # provider email, but not always: the system account holds "system".
    username: Mapped[str] = mapped_column(CITEXT, unique=True)
    # No updated_at: an account is insert-once — username never changes and there
    # is no other field to mutate — so the column would only ever equal
    # created_at, and nothing reads it.
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    @classmethod
    def get(cls, account_id: str) -> "Account | None":
        return db_session().get(cls, account_id)

    @classmethod
    def get_for_username(cls, username: str) -> "Account | None":
        return db_session().scalar(select(cls).where(cls.username == username))

    @classmethod
    def get_or_create(cls, username: str) -> "Account":
        """Concurrency-safe lookup-or-create: racing requests both INSERT with
        ON CONFLICT DO NOTHING, then converge on the one row through the
        canonical CITEXT lookup."""
        account = cls.get_for_username(username)
        if account:
            return account
        session = db_session()
        session.execute(
            insert(cls)
            .values(username=username)
            .on_conflict_do_nothing(index_elements=["username"])
        )
        return session.scalars(select(cls).where(cls.username == username)).one()

    @classmethod
    def list_non_system(cls) -> list["Account"]:
        stmt = select(cls).where(cls.username != SYSTEM_ACCOUNT_ID).order_by(cls.created_at)
        return list(db_session().scalars(stmt))


def _hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def _new_prefix() -> str:
    return "".join(secrets.choice(PAT_PREFIX_ALPHABET) for _ in range(PAT_PREFIX_LENGTH))


class PersonalAccessToken(Base, Uuid7Pk):
    __tablename__ = "personal_access_tokens"
    __table_args__ = (
        Index("personal_access_tokens_account_idx", "account_id", "revoked_at", "created_at"),
    )

    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    account: Mapped[Account] = relationship(lazy="joined", innerjoin=True)
    name: Mapped[str] = mapped_column(String(PAT_NAME_LENGTH))
    token_prefix: Mapped[str] = mapped_column(String(PAT_PREFIX_LENGTH), unique=True, index=True)
    # SHA-256 of the full serialized token; the plaintext is never stored.
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    expires_at: Mapped[datetime]
    last_used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]

    @property
    def is_expired(self) -> bool:
        return Base.utc_now() >= self.expires_at

    @property
    def status(self) -> str:
        # One tri-state on the wire; revoked outranks expired outranks active.
        if self.revoked_at:
            return "revoked"
        if self.is_expired:
            return "expired"
        return "active"

    @classmethod
    def get(cls, pat_id: str) -> "PersonalAccessToken | None":
        return db_session().get(cls, pat_id)

    @classmethod
    def get_for_prefix(cls, prefix: str) -> "PersonalAccessToken | None":
        return db_session().scalar(select(cls).where(cls.token_prefix == prefix))

    @classmethod
    def list_for_account(cls, account_id: str) -> list["PersonalAccessToken"]:
        stmt = select(cls).where(cls.account_id == account_id).order_by(cls.created_at.desc())
        return list(db_session().scalars(stmt))

    @classmethod
    def create(cls, *, account_id: str, name: str) -> "tuple[PersonalAccessToken, str]":
        """Mint ``account_id`` a token; returns (row, plaintext). The plaintext
        is shown exactly once — only its hash lands in the row."""
        prefix = _new_prefix()
        while cls.get_for_prefix(prefix):
            prefix = _new_prefix()
        secret = base64.urlsafe_b64encode(secrets.token_bytes(PAT_SECRET_BYTES))
        token = f"{PAT_TOKEN_TAG}_{prefix}_{secret.rstrip(b'=').decode()}"
        # One clock read: expires_at is exactly created_at + the lifetime.
        now = Base.utc_now()
        row = cls(
            account_id=account_id,
            name=name,
            token_prefix=prefix,
            token_hash=_hash_token(token),
            created_at=now,
            expires_at=now + PAT_LIFETIME,
        )
        session = db_session()
        session.add(row)
        session.flush()
        return row, token

    @classmethod
    def authenticate(cls, credential: str) -> "PersonalAccessToken":
        """Resolve a presented bearer credential to its live row — the one
        authentication door for both HTTP and MCP — or raise InvalidPatError.
        Stamps last_used_at, at most hourly."""
        prefix, _, _ = credential.removeprefix(f"{PAT_TOKEN_TAG}_").partition("_")
        row = cls.get_for_prefix(prefix)
        if not row:
            raise InvalidPatError("Not a recognized personal access token.")
        if not hmac.compare_digest(_hash_token(credential), row.token_hash):
            raise InvalidPatError("Not a recognized personal access token.")
        if row.revoked_at:
            raise InvalidPatError(f"Token {row.token_prefix} was revoked.")
        if row.is_expired:
            raise InvalidPatError(f"Token {row.token_prefix} has expired.")
        now = Base.utc_now()
        if not row.last_used_at or now - row.last_used_at >= PAT_LAST_USED_RESOLUTION:
            row.last_used_at = now
            db_session().flush()
        return row

    def revoke(self) -> None:
        # Keep the first revocation instant — a repeat revoke changes nothing.
        self.revoked_at = self.revoked_at or Base.utc_now()
        db_session().flush()
