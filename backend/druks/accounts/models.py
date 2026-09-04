import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import ForeignKey, Index, LargeBinary, String, select
from sqlalchemy.dialects.postgresql import CITEXT, insert
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
    SYSTEM_ACCOUNT_ID,
)
from druks.accounts.exceptions import InvalidPatError
from druks.core.models import Uuid7Pk, uuid7_str
from druks.database import db_session
from druks.models import Base
from druks.redis import get_client
from druks.sandbox.constants import MAX_AGENT_TIMEOUT_SECONDS


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
    async def get(cls, account_id: str, *, exclude_system: bool = False) -> "Account | None":
        if exclude_system and account_id == SYSTEM_ACCOUNT_ID:
            return
        return await db_session().get(cls, account_id)

    @classmethod
    async def get_for_username(cls, username: str) -> "Account | None":
        return await db_session().scalar(select(cls).where(cls.username == username))

    @classmethod
    async def get_or_create(cls, username: str) -> "Account":
        """Concurrency-safe lookup-or-create: racing requests both INSERT with
        ON CONFLICT DO NOTHING, then converge on the one row through the
        canonical CITEXT lookup."""
        account = await cls.get_for_username(username)
        if account:
            return account
        session = db_session()
        await session.execute(
            insert(cls)
            .values(username=username)
            .on_conflict_do_nothing(index_elements=["username"])
        )
        return (await session.scalars(select(cls).where(cls.username == username))).one()

    @classmethod
    async def list_non_system(cls) -> list["Account"]:
        stmt = select(cls).where(cls.username != SYSTEM_ACCOUNT_ID).order_by(cls.created_at)
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
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=_operator_api, raise_app_exceptions=False),
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
