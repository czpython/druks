import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ForeignKey, Index, LargeBinary, String, select, text
from sqlalchemy.dialects.postgresql import CITEXT, insert
from sqlalchemy.orm import Mapped, mapped_column, relationship

from druks.accounts.constants import (
    OPERATOR_TOKEN_CALL_PREFIX,
    OPERATOR_TOKEN_PREFIX,
    OPERATOR_TOKEN_TAG,
    PAT_LAST_USED_RESOLUTION,
    PAT_LIFETIME,
    PAT_NAME_LENGTH,
    PAT_PREFIX_ALPHABET,
    PAT_PREFIX_LENGTH,
    PAT_SECRET_BYTES,
    PAT_TOKEN_TAG,
)
from druks.accounts.enums import OperatorWrites
from druks.accounts.exceptions import AuthConfigurationError, InvalidPatError
from druks.core.models import Uuid7Pk
from druks.database import db_session
from druks.models import Base
from druks.redis import get_client
from druks.sandbox.constants import MAX_AGENT_TIMEOUT_SECONDS
from druks.settings import load_settings
from druks.user_settings.models import InstallationSettings


class Account(Base, Uuid7Pk):
    __tablename__ = "accounts"
    __table_args__ = (
        Index(
            "accounts_default_idx", "is_default", unique=True, postgresql_where=text("is_default")
        ),
    )

    # citext: the column compares and enforces uniqueness case-insensitively,
    # so a lookup or a duplicate check needs no normalization — the username is
    # stored as the provider gave it and matched regardless of case.
    username: Mapped[str] = mapped_column(CITEXT, unique=True)
    is_default: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    timezone: Mapped[str] = mapped_column(String, default="UTC")
    gate_park_destination_id: Mapped[str | None] = mapped_column(
        ForeignKey("notification_destinations.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    @classmethod
    async def get(cls, account_id: str) -> "Account | None":
        return await db_session().get(cls, account_id)

    @classmethod
    async def get_default(cls) -> "Account | None":
        """The unattended account, or None before account setup."""
        return await db_session().scalar(select(cls).where(cls.is_default))

    @classmethod
    async def get_for_run(cls, account_id: str | None) -> "Account":
        """Resolve the supplied account or the default before a run starts."""
        account = await cls.get(account_id) if account_id else await cls.get_default()
        if not account:
            raise AuthConfigurationError("No run account is available. Complete account setup.")
        return account

    @classmethod
    async def get_for_username(cls, username: str) -> "Account | None":
        return await db_session().scalar(select(cls).where(cls.username == username))

    @classmethod
    async def get_or_create(cls, username: str) -> "Account":
        """Return the account, or create it. The first account becomes the default."""
        account = await cls.get_for_username(username)
        if account:
            return account
        installation = await InstallationSettings.get()
        session = db_session()
        await session.execute(
            insert(cls)
            .values(
                username=username,
                is_default=~select(cls.id).exists(),
                timezone=load_settings().timezone,
                gate_park_destination_id=installation.gate_park_destination_id,
            )
            .on_conflict_do_nothing(index_elements=["username"])
        )
        return (await session.scalars(select(cls).where(cls.username == username))).one()

    async def update_preferences(self, **fields: object) -> None:
        for field, value in fields.items():
            setattr(self, field, value)
        await db_session().flush()

    @classmethod
    async def list_all(cls) -> list["Account"]:
        stmt = select(cls).order_by(cls.created_at, cls.id)
        return list(await db_session().scalars(stmt))


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
    async def get(cls, pat_id: str) -> "PersonalAccessToken | None":
        return await db_session().get(cls, pat_id)

    @classmethod
    async def get_for_prefix(cls, prefix: str) -> "PersonalAccessToken | None":
        return await db_session().scalar(select(cls).where(cls.token_prefix == prefix))

    @classmethod
    async def list_for_account(cls, account_id: str) -> list["PersonalAccessToken"]:
        stmt = select(cls).where(cls.account_id == account_id).order_by(cls.created_at.desc())
        return list(await db_session().scalars(stmt))

    @classmethod
    async def create(cls, *, account_id: str, name: str) -> "tuple[PersonalAccessToken, str]":
        """Mint ``account_id`` a token; returns (row, plaintext). The plaintext
        is shown exactly once — only its hash lands in the row."""
        prefix = _new_prefix()
        while await cls.get_for_prefix(prefix):
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
        await session.flush()
        return row, token

    @classmethod
    async def authenticate(cls, credential: str) -> "PersonalAccessToken":
        """Resolve a presented bearer credential to its live row — the one
        authentication door for both HTTP and MCP — or raise InvalidPatError.
        Stamps last_used_at, at most hourly."""
        prefix, _, _ = credential.removeprefix(f"{PAT_TOKEN_TAG}_").partition("_")
        row = await cls.get_for_prefix(prefix)
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
            await db_session().flush()
        return row

    async def revoke(self) -> None:
        # Keep the first revocation instant — a repeat revoke changes nothing.
        self.revoked_at = self.revoked_at or Base.utc_now()
        await db_session().flush()


def _hash_operator_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class OperatorToken:
    """A call-scoped bearer with PAT authority, minted for one agent call so a
    box can operate this appliance as the run's operator. Redis holds it and
    ``revoke`` drops it when the call ends; Settings never holds it."""

    account_id: str
    agent_call_id: str
    run_id: str
    writes: str

    @classmethod
    async def mint(
        cls,
        *,
        account_id: str,
        agent_call_id: str,
        run_id: str,
        writes: OperatorWrites,
    ) -> str:
        token = f"{OPERATOR_TOKEN_TAG}_{secrets.token_urlsafe(32)}"
        payload = json.dumps(
            {
                "account_id": account_id,
                "agent_call_id": agent_call_id,
                "run_id": run_id,
                "writes": writes,
            }
        )
        digest = _hash_operator_token(token)
        redis = get_client()
        # The TTL is only a backstop for a call that never reaches its revoke.
        await redis.set(f"{OPERATOR_TOKEN_PREFIX}{digest}", payload, ex=MAX_AGENT_TIMEOUT_SECONDS)
        await redis.set(
            f"{OPERATOR_TOKEN_CALL_PREFIX}{agent_call_id}", digest, ex=MAX_AGENT_TIMEOUT_SECONDS
        )
        return token

    @classmethod
    async def lookup(cls, credential: str) -> "OperatorToken | None":
        """The live token behind a credential. None for a credential of any
        other kind, so the caller can try the next door."""
        if credential.startswith(f"{OPERATOR_TOKEN_TAG}_"):
            digest = _hash_operator_token(credential)
            raw = await get_client().get(f"{OPERATOR_TOKEN_PREFIX}{digest}")
            return cls(**json.loads(raw)) if raw else None
        return

    @classmethod
    async def revoke(cls, agent_call_id: str) -> None:
        redis = get_client()
        call_key = f"{OPERATOR_TOKEN_CALL_PREFIX}{agent_call_id}"
        digest = await redis.get(call_key)
        if digest:
            await redis.delete(f"{OPERATOR_TOKEN_PREFIX}{digest.decode()}", call_key)
