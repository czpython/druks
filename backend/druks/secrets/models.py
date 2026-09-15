from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import ColumnElement, ForeignKey, Index, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy_encrypted_field import EncryptedJsonField

from druks.core.models import Uuid7Pk
from druks.database import get_session
from druks.models import Base
from druks.secrets.enums import IdentityStatus, SecretKind
from druks.secrets.exceptions import SecretRevokedError

if TYPE_CHECKING:
    from druks.accounts.models import Account


class VaultSecret(Base, Uuid7Pk):
    """A pasted value, an OAuth connection, a GitHub App key, or a subscription."""

    __tablename__ = "vault"
    __table_args__ = (
        # An account can hold many OAuth connections at one audience.
        Index(
            "ix_vault_one_per_audience",
            "kind",
            "audience",
            "account_id",
            "header",
            unique=True,
            postgresql_where=text("kind <> 'oauth'"),
            postgresql_nulls_not_distinct=True,
        ),
    )

    kind: Mapped[str]
    # Empty for the appliance's own.
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    account: Mapped["Account | None"] = relationship(lazy="joined")
    # What the secret authenticates at: a provider, a service, or an MCP server.
    audience: Mapped[str]
    # The request header the value goes in. Empty when the catalog decides.
    header: Mapped[str] = mapped_column(default="")
    secrets = EncryptedJsonField()
    # Non-secret facts: the App slug, the subscription's email.
    identity: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    identity_status: Mapped[str | None]
    identity_error: Mapped[str | None]
    # What the provider granted, for an OAuth connection.
    scopes: Mapped[list[str] | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=Base.utc_now, onupdate=Base.utc_now)
    expires_at: Mapped[datetime | None]
    # Only a subscription rotation sets it.
    last_refreshed_at: Mapped[datetime | None]
    # Revoking is a state, never a deletion: the facts stay, the secrets go.
    revoked_at: Mapped[datetime | None]
    revoked_reason: Mapped[str] = mapped_column(default="")

    @classmethod
    async def reload(cls, session: AsyncSession, secret_id: str) -> "VaultSecret | None":
        """The live row, read past the identity map, so a refresher never
        presents a token that a peer already rotated."""
        return await session.scalar(
            select(cls)
            .where(cls.id == secret_id, cls.revoked_at.is_(None))
            .execution_options(populate_existing=True)
        )

    @classmethod
    async def lookup(
        cls,
        session: AsyncSession,
        kind: SecretKind,
        audience: str,
        account_id: str | None = None,
        header: str = "",
    ) -> "VaultSecret | None":
        """The live row of a kind that keeps one per audience, account, and header."""
        return await session.scalar(
            select(cls).where(
                cls.kind == kind,
                cls.audience == audience,
                cls.account_id == account_id,
                cls.header == header,
                cls.revoked_at.is_(None),
            )
        )

    @classmethod
    async def list_keys(cls, session: AsyncSession) -> list["VaultSecret"]:
        """The installation's provider API keys."""
        return await cls._list(session, SecretKind.STATIC, cls.audience.startswith("provider:"))

    @classmethod
    async def list_tokens(
        cls, session: AsyncSession, audience: str | None = None
    ) -> list["VaultSecret"]:
        """The MCP bearer tokens and secret headers: one server's, or every server's."""
        where = cls.audience == audience if audience else cls.audience.startswith("mcp:")
        return await cls._list(session, SecretKind.STATIC, where)

    @classmethod
    async def list_installation_tokens(cls, session: AsyncSession) -> list["VaultSecret"]:
        """The MCP bearers and secret headers the installation holds."""
        return await cls._list(
            session, SecretKind.STATIC, cls.audience.startswith("mcp:"), cls.account_id.is_(None)
        )

    @classmethod
    async def list_subscriptions(
        cls,
        session: AsyncSession,
        audience: str | None = None,
        *,
        account_id: str | None = None,
        include_revoked: bool = False,
    ) -> list["VaultSecret"]:
        """The provider subscriptions: at one provider, of one account, or all.
        ``include_revoked`` adds provider revocations, never a Disconnect."""
        clauses = []
        if audience:
            clauses.append(cls.audience == audience)
        if account_id:
            clauses.append(cls.account_id == account_id)
        if include_revoked:
            clauses.append(cls.revoked_reason != "user")
        return await cls._list(
            session, SecretKind.SUBSCRIPTION, *clauses, include_revoked=include_revoked
        )

    @classmethod
    async def _list(
        cls,
        session: AsyncSession,
        kind: SecretKind,
        *clauses: ColumnElement[bool],
        include_revoked: bool = False,
    ) -> list["VaultSecret"]:
        query = select(cls).where(cls.kind == kind, *clauses)
        if not include_revoked:
            query = query.where(cls.revoked_at.is_(None))
        return list(await session.scalars(query.order_by(cls.audience, cls.id)))

    @classmethod
    async def store(
        cls,
        session: AsyncSession,
        kind: SecretKind,
        audience: str,
        *,
        secrets: dict[str, Any],
        identity: dict[str, Any] | None = None,
        account_id: str | None = None,
        header: str = "",
        scopes: list[str] | None = None,
        expires_at: datetime | None = None,
    ) -> "VaultSecret":
        """Store the one row per audience, account, and header. A second store
        replaces the first and revives a revoked row."""
        row = await session.scalar(
            select(cls).where(
                cls.kind == kind,
                cls.audience == audience,
                cls.account_id == account_id,
                cls.header == header,
            )
        )
        if not row:
            row = cls(kind=kind, audience=audience, account_id=account_id, header=header)
            session.add(row)
        row.secrets = secrets
        row.identity = identity or {}
        row.scopes = scopes or []
        row.expires_at = expires_at
        row.last_refreshed_at = None
        row.revoked_at = None
        row.revoked_reason = ""
        row.updated_at = Base.utc_now()
        await session.flush()
        return row

    @classmethod
    async def paste(
        cls,
        session: AsyncSession,
        audience: str,
        value: str,
        *,
        pasted_by: "Account",
        header: str = "",
    ) -> "VaultSecret":
        """Store a value the operator pasted for an audience, and who pasted it."""
        return await cls.store(
            session,
            SecretKind.STATIC,
            audience,
            secrets={"value": value},
            identity={"pasted_by": pasted_by.id},
            header=header,
        )

    @classmethod
    async def connect(
        cls,
        session: AsyncSession,
        audience: str,
        *,
        account_id: str | None,
        refresh_token: str,
        scopes: list[str] | None,
        identity: dict[str, Any] | None = None,
        identity_status: IdentityStatus | None = None,
        identity_error: str | None = None,
        secrets: dict[str, Any] | None = None,
    ) -> "VaultSecret":
        """A new OAuth connection. ``secrets`` holds the client it refreshes
        through, when the consent registered one."""
        row = cls(
            kind=SecretKind.OAUTH,
            audience=audience,
            account_id=account_id,
            secrets={**(secrets or {}), "refresh_token": refresh_token},
            scopes=scopes,
            identity=identity or {},
            identity_status=identity_status,
            identity_error=identity_error,
        )
        session.add(row)
        await session.flush()
        return row

    async def reconnect(
        self,
        session: AsyncSession,
        *,
        refresh_token: str,
        scopes: list[str] | None,
        identity: dict[str, Any] | None = None,
        identity_status: IdentityStatus | None = None,
        identity_error: str | None = None,
        secrets: dict[str, Any] | None = None,
    ) -> None:
        """A fresh consent on a live or revoked connection. The stored client
        stays unless the consent registered a new one."""
        kept = secrets or {
            key: value for key, value in self.secrets.items() if key != "refresh_token"
        }
        self.secrets = {**kept, "refresh_token": refresh_token}
        self.scopes = scopes
        if identity is not None:
            self.identity = identity
        self.identity_status = identity_status
        self.identity_error = identity_error
        self.updated_at = Base.utc_now()
        self.revoked_at = None
        self.revoked_reason = ""
        await session.flush()

    @classmethod
    async def list_connections(
        cls, session: AsyncSession, audience: str, *, include_revoked: bool = False
    ) -> list["VaultSecret"]:
        """Every account's OAuth connections at an audience."""
        query = select(cls).where(cls.kind == SecretKind.OAUTH, cls.audience == audience)
        if not include_revoked:
            query = query.where(cls.revoked_at.is_(None))
        return list(await session.scalars(query.order_by(cls.created_at)))

    @classmethod
    async def list_account_connections(
        cls, session: AsyncSession, audience: str, account_id: str | None
    ) -> list["VaultSecret"]:
        """One account's live OAuth connections at an audience. None is the appliance's own."""
        return list(
            await session.scalars(
                select(cls)
                .where(
                    cls.kind == SecretKind.OAUTH,
                    cls.audience == audience,
                    cls.account_id == account_id,
                    cls.revoked_at.is_(None),
                )
                .order_by(cls.created_at)
            )
        )

    @classmethod
    async def get_for_identity(
        cls, session: AsyncSession, audience: str, account_id: str | None, key: str, value: Any
    ) -> "VaultSecret | None":
        return (
            await session.scalars(
                select(cls)
                .where(
                    cls.kind == SecretKind.OAUTH,
                    cls.audience == audience,
                    cls.account_id == account_id,
                    cls.identity[key].astext == str(value),
                )
                .order_by(cls.revoked_at.is_(None).desc(), cls.updated_at.desc())
                .limit(1)
            )
        ).first()

    @classmethod
    async def list_owned_by(
        cls, session: AsyncSession, account_id: str | None
    ) -> list["VaultSecret"]:
        # The audit read, revoked rows included.
        return list(
            await session.scalars(
                select(cls)
                .where(cls.kind == SecretKind.OAUTH, cls.account_id == account_id)
                .order_by(cls.created_at)
            )
        )

    async def get_refresh_token(self, session: AsyncSession) -> str:
        if fresh := await VaultSecret.reload(session, self.id):
            return fresh.secrets["refresh_token"]
        # The services package imports the vault.
        from druks.services.exceptions import OauthRefreshError

        raise OauthRefreshError(self.audience_name, "the connection was revoked mid-refresh")

    async def update_refresh_token(self, session: AsyncSession, rotated: str) -> None:
        # The provider already invalidated the old token. A later rollback of the
        # enclosing transaction must not lose the new one, so this commits on its own.
        async with get_session(session.bind) as own:
            fresh = await own.get(VaultSecret, self.id)
            stored = (
                await own.execute(
                    update(VaultSecret)
                    .where(VaultSecret.id == self.id, VaultSecret.revoked_at.is_(None))
                    .values(secrets={**dict(fresh.secrets), "refresh_token": rotated})
                )
            ).rowcount
            await own.commit()
        if stored:
            # Expire, never assign: the enclosing commit must not rewrite secrets
            # over a revoke that lands between the two commits.
            session.expire(self, ["secrets"])
            return
        from druks.services.exceptions import OauthRefreshError

        raise OauthRefreshError(self.audience_name, "the connection was revoked mid-refresh")

    @property
    def audience_name(self) -> str:
        """The name behind the namespace: the provider id, the slug, the server."""
        return self.audience.partition(":")[2]

    @property
    def is_live(self) -> bool:
        return not self.revoked_at

    async def update_secrets(
        self, session: AsyncSession, secrets: dict[str, Any], *, expires_at: datetime | None
    ) -> None:
        """A rotation's write, on the live row only, so an earlier revoke keeps
        its cleared secrets."""
        await session.execute(
            update(type(self))
            .where(type(self).id == self.id, type(self).revoked_at.is_(None))
            .values(secrets=secrets, expires_at=expires_at, last_refreshed_at=Base.utc_now())
        )
        await session.refresh(self)

    async def revoke(self, session: AsyncSession, reason: str = "") -> None:
        """A repeat revoke keeps the first stamp."""
        now = Base.utc_now()
        await session.execute(
            update(VaultSecret)
            .where(VaultSecret.id == self.id, VaultSecret.revoked_at.is_(None))
            .values(revoked_at=now, revoked_reason=reason, secrets={})
        )
        self.revoked_at = self.revoked_at or now
        self.revoked_reason = self.revoked_reason or reason
        self.secrets = {}

    async def issue_token(
        self, session: AsyncSession, resource: str, *, host_id: str = ""
    ) -> tuple[str, datetime | None]:
        """The token a box fetches, and its expiry. A rotation skips the refresh
        request for ``host_id``, the box that asks."""
        if not self.is_live:
            raise SecretRevokedError(self.audience)
        if self.kind == SecretKind.STATIC:
            return self.secrets["value"], None
        if self.kind == SecretKind.APP_KEY:
            from druks.apps.registry import services

            return await services.get(self.audience_name).issue_token(resource)
        if self.kind == SecretKind.OAUTH:
            from druks.apps.registry import services

            if self.audience.startswith("mcp:"):
                from druks.mcp import oauth

                return await oauth.get_access_token(session, self.audience_name, self.account_id)
            client = await services.get(self.audience_name).get_oauth_client()
            return await client.get_access_token(session, connection=self)
        from druks.harnesses.providers import get_provider

        token = await get_provider(self.audience_name).issue_token(
            session, self.id, except_host_id=host_id
        )
        return token.access_token, token.expires_at
