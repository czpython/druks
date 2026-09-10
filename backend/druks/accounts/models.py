import base64
import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx2
from sqlalchemy import ForeignKey, Index, LargeBinary, String, select, text
from sqlalchemy.dialects.postgresql import CITEXT, JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship

from druks.accounts.constants import (
    OPERATOR_DEFERRED_PREFIX,
    OPERATOR_TOKEN_CALL_PREFIX,
    OPERATOR_TOKEN_PREFIX,
    OPERATOR_TOKEN_TAG,
    OPERATOR_WRITES,
    PAT_LAST_USED_RESOLUTION,
    PAT_LIFETIME,
    PAT_NAME_LENGTH,
    PAT_PREFIX_ALPHABET,
    PAT_PREFIX_LENGTH,
    PAT_SECRET_BYTES,
    PAT_TOKEN_TAG,
)
from druks.accounts.exceptions import AuthConfigurationError, InvalidPatError
from druks.core.models import Uuid7Pk, uuid7_str
from druks.models import Base
from druks.redis import get_client
from druks.sandbox.constants import MAX_AGENT_TIMEOUT_SECONDS
from druks.secrets.models import VaultSecret
from druks.settings import load_settings
from druks.user_settings.models import InstallationSettings

logger = logging.getLogger(__name__)


class Account(Base, Uuid7Pk):
    __tablename__ = "accounts"
    __table_args__ = (
        Index(
            "accounts_default_idx", "is_default", unique=True, postgresql_where=text("is_default")
        ),
    )

    # The citext column compares and enforces uniqueness without regard to case.
    # A lookup or a duplicate check needs no normalization. The username stays
    # as the provider gave it.
    username: Mapped[str] = mapped_column(CITEXT, unique=True)
    is_default: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    timezone: Mapped[str] = mapped_column(String, default="UTC")
    gate_park_destination_id: Mapped[str | None] = mapped_column(
        ForeignKey("notification_destinations.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    @classmethod
    async def get_default(cls, session: AsyncSession) -> "Account | None":
        """The unattended account, or None before account setup."""
        return await session.scalar(select(cls).where(cls.is_default))

    @classmethod
    async def get_for_run(cls, session: AsyncSession, account_id: str | None) -> "Account":
        """Resolve the supplied account or the default before a run starts."""
        if account_id:
            account = await session.get(cls, account_id)
        else:
            account = await cls.get_default(session)
        if not account:
            raise AuthConfigurationError("No run account is available. Complete account setup.")
        return account

    @classmethod
    async def get_for_username(cls, session: AsyncSession, username: str) -> "Account | None":
        return await session.scalar(select(cls).where(cls.username == username))

    @classmethod
    async def lookup(cls, session: AsyncSession, authority: str, subject: str) -> "Account | None":
        """The one account with a live grant for this provider user. A shared
        grant has no account, and two owning accounts match none."""
        owners = select(VaultSecret.account_id).where(
            VaultSecret.revoked_at.is_(None),
            VaultSecret.identity["authority"].astext == authority,
            VaultSecret.identity["subject"].astext == subject,
        )
        accounts = list(await session.scalars(select(cls).where(cls.id.in_(owners))))
        if len(accounts) == 1:
            return accounts[0]
        if accounts:
            logger.warning("A provider user at %s has grants under multiple accounts.", authority)
        return

    @classmethod
    async def get_or_create(cls, session: AsyncSession, username: str) -> "Account":
        """Return the account, or create it. The first account becomes the default."""
        account = await cls.get_for_username(session, username)
        if account:
            return account
        installation = await InstallationSettings.get_or_create(session)
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
        await self.session.flush()

    @classmethod
    async def list_all(cls, session: AsyncSession) -> list["Account"]:
        stmt = select(cls).order_by(cls.created_at, cls.id)
        return list(await session.scalars(stmt))


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
    # The SHA-256 digest of the full serialized token. Druks never stores the plaintext.
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    expires_at: Mapped[datetime]
    last_used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
    # None allows the whole API of the account. A list allows only the agent tools
    # that it names by MCP name.
    allowed_tools: Mapped[list[str] | None] = mapped_column(JSONB, default=None)

    @property
    def is_expired(self) -> bool:
        return Base.utc_now() >= self.expires_at

    @property
    def status(self) -> str:
        # The wire carries one of three states. Revoked outranks expired, and expired
        # outranks active.
        if self.revoked_at:
            return "revoked"
        if self.is_expired:
            return "expired"
        return "active"

    @classmethod
    async def get_for_prefix(
        cls, session: AsyncSession, prefix: str
    ) -> "PersonalAccessToken | None":
        return await session.scalar(select(cls).where(cls.token_prefix == prefix))

    @classmethod
    async def list_for_account(
        cls, session: AsyncSession, account_id: str
    ) -> list["PersonalAccessToken"]:
        stmt = select(cls).where(cls.account_id == account_id).order_by(cls.created_at.desc())
        return list(await session.scalars(stmt))

    @classmethod
    async def create(
        cls,
        session: AsyncSession,
        *,
        account_id: str,
        name: str,
        allowed_tools: list[str] | None = None,
    ) -> "tuple[PersonalAccessToken, str]":
        """Mint a token for ``account_id`` and return (row, plaintext). The row
        keeps only the hash, so the plaintext shows once."""
        prefix = _new_prefix()
        while await cls.get_for_prefix(session, prefix):
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
            allowed_tools=allowed_tools,
        )
        session.add(row)
        await session.flush()
        return row, token

    @classmethod
    async def authenticate(cls, session: AsyncSession, credential: str) -> "PersonalAccessToken":
        """Resolve a bearer credential to its live row, or raise InvalidPatError.
        HTTP and MCP both authenticate here. last_used_at updates at most hourly."""
        prefix, _, _ = credential.removeprefix(f"{PAT_TOKEN_TAG}_").partition("_")
        row = await cls.get_for_prefix(session, prefix)
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
            await session.flush()
        return row

    async def revoke(self) -> None:
        # A repeat revoke keeps the first revocation time.
        self.revoked_at = self.revoked_at or Base.utc_now()
        await self.session.flush()


def _hash_operator_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


_operator_api = None


@dataclass(frozen=True)
class OperatorToken:
    """A call-scoped bearer with PAT authority. Redis holds it for the agent
    call; Settings never does. ``writes`` is deny (read tools only), defer
    (stash mutating calls), or allow (execute as ``account_id``)."""

    account_id: str
    agent_call_id: str
    run_id: str
    writes: str

    @classmethod
    def bind_api(cls, api: Any) -> None:
        global _operator_api
        _operator_api = api

    @classmethod
    async def mint(
        cls,
        *,
        account_id: str,
        agent_call_id: str,
        run_id: str,
        writes: str,
    ) -> str:
        if writes not in OPERATOR_WRITES:
            raise ValueError(
                f"operator token writes must be one of {sorted(OPERATOR_WRITES)}, not {writes!r}"
            )
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
        await redis.set(f"{OPERATOR_TOKEN_PREFIX}{digest}", payload, ex=MAX_AGENT_TIMEOUT_SECONDS)
        await redis.set(
            f"{OPERATOR_TOKEN_CALL_PREFIX}{agent_call_id}", digest, ex=MAX_AGENT_TIMEOUT_SECONDS
        )
        return token

    @classmethod
    async def lookup(cls, credential: str) -> "OperatorToken | None":
        if not credential.startswith(f"{OPERATOR_TOKEN_TAG}_"):
            return
        raw = await get_client().get(f"{OPERATOR_TOKEN_PREFIX}{_hash_operator_token(credential)}")
        if raw:
            return cls(**json.loads(raw))
        return

    @classmethod
    async def authenticate(cls, credential: str) -> "OperatorToken":
        found = await cls.lookup(credential)
        if found:
            return found
        raise InvalidPatError("Not a recognized operator token.")

    @classmethod
    async def revoke(cls, agent_call_id: str) -> None:
        redis = get_client()
        call_key = f"{OPERATOR_TOKEN_CALL_PREFIX}{agent_call_id}"
        digest = await redis.get(call_key)
        if digest:
            await redis.delete(f"{OPERATOR_TOKEN_PREFIX}{digest.decode()}", call_key)

    @classmethod
    async def defer_write(cls, run_id: str, write: dict[str, str]) -> None:
        redis = get_client()
        key = f"{OPERATOR_DEFERRED_PREFIX}{run_id}"
        await redis.rpush(key, json.dumps(write))
        await redis.expire(key, MAX_AGENT_TIMEOUT_SECONDS)

    @classmethod
    async def take_deferred(cls, run_id: str) -> list[dict[str, str]]:
        redis = get_client()
        key = f"{OPERATOR_DEFERRED_PREFIX}{run_id}"
        items = await redis.lrange(key, 0, -1)
        await redis.delete(key)
        return [json.loads(item) for item in items]

    @classmethod
    async def play_deferred(cls, account_id: str, writes: list[dict[str, str]]) -> None:
        if not _operator_api:
            raise RuntimeError("operator token replay needs the API bound at MCP boot")
        call_id = uuid7_str()
        token = await cls.mint(
            account_id=account_id, agent_call_id=call_id, run_id=call_id, writes="allow"
        )
        try:
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=_operator_api, raise_app_exceptions=False),
                base_url="http://druks",
            ) as client:
                for write in writes:
                    response = await client.request(
                        write["method"],
                        write["path"],
                        content=write["body"] or None,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Content-Type": write["content_type"],
                        },
                    )
                    if response.status_code >= 400:
                        raise RuntimeError(
                            f"deferred {write['method']} {write['path']} failed: "
                            f"{response.status_code} {response.text}"
                        )
        finally:
            await cls.revoke(call_id)
