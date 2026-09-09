from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, String, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from druks.apps.registry import mcp_servers
from druks.core.models import Uuid7Pk
from druks.database import db_session
from druks.mcp.constants import BEARER_HEADER, NAME_PATTERN
from druks.mcp.enums import TokenSource
from druks.mcp.exceptions import InvalidServerNameError
from druks.mcp.helpers import get_grant_account
from druks.models import Base
from druks.secrets.datastructures import Audience
from druks.secrets.enums import SecretKind
from druks.secrets.models import VaultSecret


class McpServer(Base, Uuid7Pk):
    __tablename__ = "mcp_servers"

    # A row is the operator's overlay: a custom server they added, or a built-in
    # they set state on. Either carries its own url — a built-in overlay copies
    # the url from the built-in def when the operator's choice first creates it.
    name: Mapped[str] = mapped_column(String, unique=True)
    url: Mapped[str] = mapped_column(String)
    # How delivery sources this row's Authorization bearer (a TokenSource), or
    # "" for no bearer — the server authenticates through its declared headers,
    # or takes none. A catalog-managed name reads its source from the registry
    # definition instead.
    token_source: Mapped[str] = mapped_column(String, default=TokenSource.STATIC)
    # The plain declared header values from the server's spec. A secret one,
    # like the bearer itself, is a vault row at the server's audience.
    headers: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
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
        # fully custom rows. A secret is the vault row itself; its value is
        # read where it enters a run.
        rows = {server.name: server for server in await cls.list_all()}
        tokens: dict[str, VaultSecret] = {}
        secret_headers: dict[str, dict[str, VaultSecret]] = {}
        for secret in await VaultSecret.list_tokens():
            if secret.header == BEARER_HEADER:
                tokens[secret.audience_name] = secret
            else:
                secret_headers.setdefault(secret.audience_name, {})[secret.header] = secret
        servers: dict[str, dict] = {}
        for definition in mcp_servers.all():
            name = definition["name"]
            row = rows.pop(name, None)
            servers[name] = {
                "name": name,
                "url": definition["url"],
                "token_source": definition["token_source"],
                "is_enabled": row.is_enabled if row else definition["enabled"],
                "token": tokens.get(name),
                "headers": row.headers if row else {},
                "secret_headers": secret_headers.get(name, {}),
                "identity_mode": row.identity_mode if row else None,
                "builtin": True,
            }
        for row in rows.values():
            servers[row.name] = {
                "name": row.name,
                "url": row.url,
                "token_source": row.token_source,
                "is_enabled": row.is_enabled,
                "token": tokens.get(row.name),
                "headers": row.headers,
                "secret_headers": secret_headers.get(row.name, {}),
                "identity_mode": row.identity_mode,
                "builtin": False,
            }
        return servers

    @classmethod
    async def get_resolved(cls, account_id: str | None) -> dict[str, dict]:
        servers = await cls._merged()
        # has_token = nothing blocks this server's auth at delivery, read from
        # wherever its source keeps the secret: a stored grant for a connected
        # server, the stored token for a static one; a bearerless server has
        # none to miss.
        for server in servers.values():
            source = server["token_source"]
            if not source:
                server["has_token"] = True
            elif source == TokenSource.OAUTH:
                server["has_token"] = False
                if server["identity_mode"]:
                    grant_account = get_grant_account(server["identity_mode"], account_id)
                    server["has_token"] = bool(
                        await VaultSecret.list_account_connections(
                            Audience.mcp(server["name"]), grant_account
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
            token_source=token_source,
            headers=headers or {},
            is_enabled=is_enabled,
        )
        session.add(server)
        await session.flush()
        audience = Audience.mcp(name)
        if token:
            await VaultSecret.store(
                SecretKind.STATIC, audience, header=BEARER_HEADER, secrets={"value": token}
            )
        for header, value in (secret_headers or {}).items():
            await VaultSecret.store(
                SecretKind.STATIC, audience, header=header, secrets={"value": value}
            )
        return server

    async def delete(self) -> None:
        for secret in await VaultSecret.list_tokens(Audience.mcp(self.name)):
            await secret.revoke("server_removed")
        session = db_session()
        await session.delete(self)
        await session.flush()
