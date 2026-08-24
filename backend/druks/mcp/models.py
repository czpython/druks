import os
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Mapped, mapped_column

from druks.accounts.constants import SYSTEM_ACCOUNT_ID
from druks.apps.registry import mcp_servers
from druks.core.models import Uuid7Pk
from druks.database import db_session
from druks.mcp.constants import NAME_PATTERN
from druks.mcp.enums import TokenSource
from druks.mcp.exceptions import InvalidServerNameError
from druks.mcp.helpers import get_grant_account, grant_provider
from druks.models import Base
from druks.secrets.fields import EncryptedJsonField, EncryptedTextField, Secret
from druks.services.models import OauthConnection


class McpServer(Base, Uuid7Pk):
    __tablename__ = "mcp_servers"

    # A row is the operator's overlay: a custom server they added, or a built-in
    # they set state on. Either carries its own url — a built-in overlay copies
    # the url from the built-in def when the operator's choice first creates it.
    name: Mapped[str] = mapped_column(String, unique=True)
    url: Mapped[str] = mapped_column(String)
    token = EncryptedTextField(default="")
    # How delivery sources this row's Authorization bearer (a TokenSource), or
    # "" for no bearer — the server authenticates through its declared headers,
    # or takes none. A catalog-managed name reads its source from the registry
    # definition instead; static_from_env exists only there.
    token_source: Mapped[str] = mapped_column(String, default=TokenSource.STATIC)
    # Declared header values from the server's spec, split by secrecy at
    # install time — the split *is* the secrecy record delivery and the API
    # read from: plain values inline, secret ones ciphertext at rest.
    headers: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    secret_headers = EncryptedJsonField()
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # The first completed OAuth connect claims the credential-sharing policy.
    # A registry install or enable overlay alone carries no such decision.
    identity_mode: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    @classmethod
    async def list_all(cls) -> list["McpServer"]:
        # The raw overlay rows — not the merged registry view (_merged).
        return list((await db_session().execute(select(cls).order_by(cls.name))).scalars())

    @classmethod
    async def get_for_name(cls, name: str) -> "McpServer | None":
        return (
            await db_session().execute(select(cls).where(cls.name == name))
        ).scalar_one_or_none()

    @classmethod
    async def _merged(cls) -> dict[str, dict]:
        # The full view the API and delivery build from, keyed by
        # name: each built-in definition (url + auth from the registry)
        # overlaid with its operator row's enable choice and secrets, then any
        # fully custom rows.
        rows = {server.name: server for server in await cls.list_all()}
        servers: dict[str, dict] = {}
        for definition in mcp_servers.all():
            row = rows.pop(definition["name"], None)
            servers[definition["name"]] = {
                "name": definition["name"],
                "url": definition["url"],
                "token_source": definition["token_source"],
                "source_env_var": definition["source_env_var"],
                "is_enabled": row.is_enabled if row else definition["enabled"],
                "token": row.token if row else Secret(b"", ""),
                "headers": row.headers if row else {},
                "secret_headers": row.secret_headers if row else {},
                "identity_mode": row.identity_mode if row else None,
                "builtin": True,
            }
        for row in rows.values():
            servers[row.name] = {
                "name": row.name,
                "url": row.url,
                "token_source": row.token_source,
                "source_env_var": "",
                "is_enabled": row.is_enabled,
                "token": row.token,
                "headers": row.headers,
                "secret_headers": row.secret_headers,
                "identity_mode": row.identity_mode,
                "builtin": False,
            }
        return servers

    @classmethod
    async def get_resolved(cls, account_id: str | None) -> dict[str, dict]:
        servers = await cls._merged()
        # has_token = nothing blocks this server's auth at delivery, read from
        # wherever its source keeps the secret: druks' env for an env-sourced
        # server, a stored grant for a connected one, the stored token for a
        # static one; a bearerless server has none to miss.
        for server in servers.values():
            source = server["token_source"]
            if not source:
                server["has_token"] = True
            elif source == TokenSource.STATIC_FROM_ENV:
                server["has_token"] = bool(os.environ.get(server["source_env_var"]))
            elif source == TokenSource.OAUTH:
                server["has_token"] = False
                if server["identity_mode"]:
                    grant_account = get_grant_account(server["identity_mode"], account_id)
                    server["has_token"] = bool(
                        await OauthConnection.list_for_account(
                            grant_provider(server["name"]), grant_account
                        )
                    )
            else:
                server["has_token"] = bool(server["token"])
        return servers

    @classmethod
    async def list_enabled(cls) -> list[dict]:
        # The enabled subset — what a run delivers and the settings UI shows active.
        return [server for server in (await cls._merged()).values() if server["is_enabled"]]

    @classmethod
    async def set_enabled(cls, name: str, is_enabled: bool) -> bool:
        # A built-in has no row until an operator changes its state; the enable
        # choice creates one, carrying the built-in's url. False means the name
        # is neither a row nor a catalog entry.
        server = await cls.get_for_name(name)
        if server:
            server.is_enabled = is_enabled
            return True
        if name in mcp_servers:
            await cls.create(name=name, url=mcp_servers.get(name)["url"], is_enabled=is_enabled)
            return True
        return False

    @classmethod
    async def create(
        cls,
        *,
        name: str,
        url: str,
        token: str = "",
        token_source: str = TokenSource.STATIC,
        headers: dict[str, str] | None = None,
        secret_headers: dict[str, str] | None = None,
        is_enabled: bool = True,
    ) -> "McpServer":
        if not NAME_PATTERN.match(name):
            raise InvalidServerNameError(name)
        session = db_session()
        server = cls(
            name=name,
            url=url,
            token=token,
            token_source=token_source,
            headers=headers or {},
            secret_headers=secret_headers or {},
            is_enabled=is_enabled,
        )
        session.add(server)
        await session.flush()
        return server

    async def delete(self) -> None:
        session = db_session()
        await session.delete(self)
        await session.flush()


class McpClientRegistration(Base, Uuid7Pk):
    __tablename__ = "mcp_client_registrations"
    __table_args__ = (UniqueConstraint("server_id", "account_id"),)

    # One RFC 7591 registration per grant: druks registers a fresh client on
    # every connect, so each account's grant refreshes as the client it
    # consented through. The refresh token lives on the platform's OauthConnection.
    server_id: Mapped[str] = mapped_column(ForeignKey("mcp_servers.id", ondelete="CASCADE"))
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), default=SYSTEM_ACCOUNT_ID
    )
    token_endpoint: Mapped[str] = mapped_column(String)
    client_id: Mapped[str] = mapped_column(String)
    # "" for public clients (PKCE-only); some authorization servers issue one
    # even for token_endpoint_auth_method "none" and then expect it on refresh.
    client_secret = EncryptedTextField(default="")

    @classmethod
    async def get_for_account(
        cls, server_name: str, account_id: str
    ) -> "McpClientRegistration | None":
        return (
            await db_session().execute(
                select(cls)
                .join(McpServer, McpServer.id == cls.server_id)
                .where(McpServer.name == server_name, cls.account_id == account_id)
            )
        ).scalar_one_or_none()

    @classmethod
    async def store(
        cls,
        *,
        server_id: str,
        account_id: str,
        token_endpoint: str,
        client_id: str,
        client_secret: str = "",
    ) -> "McpClientRegistration":
        session = db_session()
        statement = pg_insert(cls).values(
            server_id=server_id,
            account_id=account_id,
            token_endpoint=token_endpoint,
            client_id=client_id,
            client_secret=client_secret,
        )
        statement = statement.on_conflict_do_update(
            index_elements=["server_id", "account_id"],
            set_={
                "token_endpoint": statement.excluded.token_endpoint,
                "client_id": statement.excluded.client_id,
                "client_secret": statement.excluded.client_secret,
            },
        ).returning(cls)
        return (
            await session.scalars(statement, execution_options={"populate_existing": True})
        ).one()

    async def delete(self) -> None:
        session = db_session()
        await session.delete(self)
        await session.flush()
