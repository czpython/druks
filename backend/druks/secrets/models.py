from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import ColumnElement, ForeignKey, Index, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy_encrypted_field import EncryptedJsonField

from druks.core.models import Uuid7Pk
from druks.database import db_session, get_session
from druks.models import Base
from druks.secrets.enums import SecretKind
from druks.secrets.exceptions import SecretRevokedError

if TYPE_CHECKING:
    from druks.accounts.models import Account


class VaultSecret(Base, Uuid7Pk):
    """One secret Druks keeps, and the kind that turns it into a token: a
    pasted value, an OAuth connection, a GitHub App key, or a subscription."""

    __tablename__ = "vault"
    __table_args__ = (
        # One row per audience, account, and header for a pasted value, an App
        # key, or a subscription. An OAuth connection is the exception: an
        # account can hold many at one audience, one per mailbox or workspace.
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
    # The account whose secret this is. Empty for the appliance's own.
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    account: Mapped["Account | None"] = relationship(lazy="joined")
    # What the secret authenticates at: a provider, a service, or an MCP server.
    audience: Mapped[str]
    # The request header the value goes in. Empty when the catalog decides.
    header: Mapped[str] = mapped_column(default="")
    secrets = EncryptedJsonField()
    # Non-secret facts: the App slug, the subscription's email.
    identity: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # What the provider granted, for an OAuth connection.
    scopes: Mapped[list[str]] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=Base.utc_now, onupdate=Base.utc_now)
    expires_at: Mapped[datetime | None]
    # Revoking is a state, never a deletion: the facts stay, the secrets go.
    revoked_at: Mapped[datetime | None]
    revoked_reason: Mapped[str] = mapped_column(default="")

    @classmethod
    async def get(cls, secret_id: str) -> "VaultSecret | None":
        # Any state: a revoked row keeps its facts, and a caller reads is_live.
        return await db_session().get(cls, secret_id)

    @classmethod
    async def reload(cls, secret_id: str) -> "VaultSecret | None":
        """One live row read past the identity map: what a refresher reads
        after it wins the lock, so it never presents a token a peer advanced."""
        return await db_session().scalar(
            select(cls)
            .where(cls.id == secret_id, cls.revoked_at.is_(None))
            .execution_options(populate_existing=True)
        )

    @classmethod
    async def lookup(
        cls, kind: SecretKind, audience: str, account_id: str | None = None, header: str = ""
    ) -> "VaultSecret | None":
        """The live row of a kind that keeps one per audience, account, and header."""
        return await db_session().scalar(
            select(cls).where(
                cls.kind == kind,
                cls.audience == audience,
                cls.account_id == account_id,
                cls.header == header,
                cls.revoked_at.is_(None),
            )
        )

    @classmethod
    async def list_keys(cls) -> list["VaultSecret"]:
        """The installation's provider API keys."""
        return await cls._list(SecretKind.STATIC, cls.audience.startswith("provider:"))

    @classmethod
    async def list_tokens(cls, audience: str | None = None) -> list["VaultSecret"]:
        """The MCP bearer tokens and secret headers: one server's, or every server's."""
        where = cls.audience == audience if audience else cls.audience.startswith("mcp:")
        return await cls._list(SecretKind.STATIC, where)

    @classmethod
    async def list_subscriptions(
        cls, audience: str | None = None, *, account_id: str | None = None
    ) -> list["VaultSecret"]:
        """The provider subscriptions: at one provider, of one account, or all."""
        clauses = []
        if audience:
            clauses.append(cls.audience == audience)
        if account_id:
            clauses.append(cls.account_id == account_id)
        return await cls._list(SecretKind.SUBSCRIPTION, *clauses)

    @classmethod
    async def _list(cls, kind: SecretKind, *clauses: ColumnElement[bool]) -> list["VaultSecret"]:
        query = select(cls).where(cls.kind == kind, cls.revoked_at.is_(None), *clauses)
        return list(await db_session().scalars(query.order_by(cls.audience, cls.id)))

    @classmethod
    async def store(
        cls,
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
        """Store the one row a kind keeps per audience, account, and header. A
        second store replaces the first, and a revoked row comes back to life."""
        row = await db_session().scalar(
            select(cls).where(
                cls.kind == kind,
                cls.audience == audience,
                cls.account_id == account_id,
                cls.header == header,
            )
        )
        if not row:
            row = cls(kind=kind, audience=audience, account_id=account_id, header=header)
            db_session().add(row)
        row.secrets = secrets
        row.identity = identity or {}
        row.scopes = scopes or []
        row.expires_at = expires_at
        row.revoked_at = None
        row.revoked_reason = ""
        row.updated_at = Base.utc_now()
        await db_session().flush()
        return row

    @classmethod
    async def paste(
        cls, audience: str, value: str, *, pasted_by: "Account", header: str = ""
    ) -> "VaultSecret":
        """Store a value the operator pasted for an audience, and who pasted it."""
        return await cls.store(
            SecretKind.STATIC,
            audience,
            secrets={"value": value},
            identity={"pasted_by": pasted_by.id},
            header=header,
        )

    @classmethod
    async def connect(
        cls,
        audience: str,
        *,
        account_id: str | None,
        refresh_token: str,
        scopes: list[str],
        identity: dict[str, Any] | None = None,
        secrets: dict[str, Any] | None = None,
    ) -> "VaultSecret":
        """A new OAuth connection at an audience: the consent's refresh token,
        and the client it refreshes through when the audience registered one.
        An account can hold many connections at one audience."""
        row = cls(
            kind=SecretKind.OAUTH,
            audience=audience,
            account_id=account_id,
            secrets={**(secrets or {}), "refresh_token": refresh_token},
            scopes=scopes,
            identity=identity or {},
        )
        db_session().add(row)
        await db_session().flush()
        return row

    async def reconnect(
        self,
        *,
        refresh_token: str,
        scopes: list[str],
        identity: dict[str, Any] | None = None,
        secrets: dict[str, Any] | None = None,
    ) -> None:
        """A revoked connection, or a live one, takes a fresh consent. The
        client it refreshes through stays unless the consent registered a new one."""
        kept = secrets or {
            key: value for key, value in self.secrets.items() if key != "refresh_token"
        }
        self.secrets = {**kept, "refresh_token": refresh_token}
        self.scopes = scopes
        if identity:
            self.identity = identity
        self.updated_at = Base.utc_now()
        self.revoked_at = None
        self.revoked_reason = ""
        await db_session().flush()

    @classmethod
    async def list_connections(
        cls, audience: str, *, include_revoked: bool = False
    ) -> list["VaultSecret"]:
        """Every account's OAuth connections at an audience."""
        query = select(cls).where(cls.kind == SecretKind.OAUTH, cls.audience == audience)
        if not include_revoked:
            query = query.where(cls.revoked_at.is_(None))
        return list(await db_session().scalars(query.order_by(cls.created_at)))

    @classmethod
    async def list_account_connections(
        cls, audience: str, account_id: str | None
    ) -> list["VaultSecret"]:
        """One account's live OAuth connections at an audience. None is the
        appliance's own."""
        return list(
            await db_session().scalars(
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
        cls, audience: str, account_id: str | None, key: str, value: Any
    ) -> "VaultSecret | None":
        # A live match wins; among revoked matches, the latest consent wins.
        return (
            await db_session().scalars(
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
    async def list_owned_by(cls, account_id: str | None) -> list["VaultSecret"]:
        # The audit read: every connection this account ever authorized,
        # revoked rows included.
        return list(
            await db_session().scalars(
                select(cls)
                .where(cls.kind == SecretKind.OAUTH, cls.account_id == account_id)
                .order_by(cls.created_at)
            )
        )

    async def _load_refresh_token(self) -> str:
        # Under the refresh lock: another process may have rotated and
        # committed, and this transaction may already hold the row.
        # populate_existing re-reads it past the identity map.
        fresh = (
            await db_session().scalars(
                select(VaultSecret)
                .where(VaultSecret.id == self.id)
                .execution_options(populate_existing=True)
            )
        ).one()
        if fresh.revoked_at:
            # The services package imports the vault; the error is the one path back.
            from druks.services.exceptions import OauthRefreshError

            raise OauthRefreshError(self.audience_name, "the connection was revoked mid-refresh")
        return fresh.secrets["refresh_token"]

    async def _save_refresh_token(self, rotated: str) -> None:
        # The provider invalidated the old token the moment it rotated, so
        # the write commits on its own session, never the enclosing
        # transaction. A step that rolls back later must not brick the connection.
        async with get_session(db_session().bind) as session:
            fresh = await session.get(VaultSecret, self.id)
            stored = 0
            if fresh and not fresh.revoked_at:
                result = await session.execute(
                    update(VaultSecret)
                    .where(VaultSecret.id == self.id, VaultSecret.revoked_at.is_(None))
                    .values(secrets={**dict(fresh.secrets), "refresh_token": rotated})
                )
                stored = result.rowcount
            await session.commit()
        if not stored:
            # A revoke landed mid-refresh. Nothing secret outlives the
            # consent at rest, so the rotated token is not stored.
            from druks.services.exceptions import OauthRefreshError

            raise OauthRefreshError(self.audience_name, "the connection was revoked mid-refresh")
        # The next read loads the rotated value. The enclosing transaction
        # never writes this column at commit, so a revoke that lands between
        # the two commits keeps its cleared secrets.
        db_session().expire(self, ["secrets"])

    @property
    def audience_name(self) -> str:
        """The name behind the namespace: the provider id, the slug, the server."""
        return self.audience.partition(":")[2]

    @property
    def is_live(self) -> bool:
        return not self.revoked_at

    async def update_secrets(self, secrets: dict[str, Any], *, expires_at: datetime | None) -> None:
        """A rotation's write: the whole mapping and its expiry, on the live
        row only, so a revoke that landed first keeps its cleared secrets."""
        await db_session().execute(
            update(type(self))
            .where(type(self).id == self.id, type(self).revoked_at.is_(None))
            .values(secrets=secrets, expires_at=expires_at)
        )
        await db_session().refresh(self)

    async def revoke(self, reason: str = "") -> None:
        # A second revoke keeps the first stamp.
        self.revoked_at = self.revoked_at or Base.utc_now()
        self.revoked_reason = self.revoked_reason or reason
        self.secrets = {}
        await db_session().flush()

    async def issue_token(self, resource: str, *, host_id: str = "") -> tuple[str, datetime | None]:
        """The token a box fetches from this secret, and its expiry. A pasted
        value is itself and never expires. ``host_id`` names the box that asks,
        so a rotation skips its refresh request."""
        if not self.is_live:
            raise SecretRevokedError(self.audience)
        if self.kind == SecretKind.STATIC:
            return self.secrets["value"], None
        if self.kind == SecretKind.APP_KEY:
            # The service declares how its App key turns into a token.
            from druks.apps.registry import services

            return await services.get(self.audience_name).issue_token(resource)
        if self.kind == SecretKind.OAUTH:
            # An MCP connection refreshes through the client it registered; a
            # service connection through the service's own client.
            from druks.apps.registry import services

            if self.audience.startswith("mcp:"):
                from druks.mcp import oauth

                return await oauth.get_access_token(self.audience_name, self.account_id)
            client = await services.get(self.audience_name).get_oauth_client()
            return await client.get_access_token(connection=self)
        # The provider rotates the subscription and answers its access token.
        from druks.harnesses.providers import get_provider

        token = await get_provider(self.audience_name).issue_token(self.id, except_host_id=host_id)
        return token.access_token, token.expires_at
