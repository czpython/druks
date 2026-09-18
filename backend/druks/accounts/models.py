import base64
import hashlib
import hmac
import logging
import secrets
from datetime import datetime

from sqlalchemy import ForeignKey, Index, LargeBinary, String, select, text
from sqlalchemy.dialects.postgresql import CITEXT, JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship

from druks.accounts.constants import (
    PAT_LAST_USED_RESOLUTION,
    PAT_LIFETIME,
    PAT_NAME_LENGTH,
    PAT_PREFIX_ALPHABET,
    PAT_PREFIX_LENGTH,
    PAT_SECRET_BYTES,
    PAT_TOKEN_TAG,
)
from druks.accounts.enums import AccountKind
from druks.accounts.exceptions import AuthConfigurationError, InvalidPatError
from druks.core.models import Uuid7Pk, uuid7_str
from druks.models import Base
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
    kind: Mapped[str] = mapped_column(
        default=AccountKind.OPERATOR, server_default=text("'operator'")
    )
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
    async def get_secrets_owner(
        cls, session: AsyncSession, account_id: str | None
    ) -> "Account | None":
        """The account whose secrets an agent acts with: the given operator, or else the
        default account. A bot or bot admin account holds no secrets."""
        account = await session.get(cls, account_id) if account_id else None
        if account and account.kind == AccountKind.OPERATOR:
            return account
        return await cls.get_default(session)

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

    @classmethod
    async def create_for_bot(cls, session: AsyncSession, kind: AccountKind) -> "Account":
        """The bot or bot admin account of a Bot's channel connection, such as a linked
        number. It never signs in and never becomes the default."""
        account = cls(
            username=f"{kind}:{uuid7_str()}",
            kind=kind,
            timezone=load_settings().timezone,
        )
        session.add(account)
        await session.flush()
        return account

    async def update_preferences(self, **fields: object) -> None:
        for field, value in fields.items():
            setattr(self, field, value)
        await self.session.flush()

    @classmethod
    async def list_operators(cls, session: AsyncSession) -> list["Account"]:
        """The accounts that sign in. Bot and bot admin accounts never do."""
        stmt = select(cls).where(cls.kind == AccountKind.OPERATOR).order_by(cls.created_at, cls.id)
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
