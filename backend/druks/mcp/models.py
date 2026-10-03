from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import Boolean, String, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from druks.accounts.models import Account
from druks.apps.registry import mcp_servers, services
from druks.core.models import Uuid7Pk
from druks.mcp.constants import BEARER_HEADER, DRUKS_SERVER_NAME, NAME_PATTERN
from druks.mcp.datastructures import McpServerAccess
from druks.mcp.enums import Credential
from druks.mcp.exceptions import InvalidServerNameError, ReservedServerNameError
from druks.mcp.helpers import get_grant_account
from druks.models import Base
from druks.secrets.datastructures import Audience
from druks.secrets.enums import SecretKind
from druks.secrets.models import VaultSecret
from druks.services import Service


class McpServer(Base, Uuid7Pk):
    __tablename__ = "mcp_servers"

    # A row is the operator's overlay: a custom server they added, or a built-in
    # they set state on. Either carries its own url — a built-in overlay copies
    # the url from the built-in def when the operator's choice first creates it.
    name: Mapped[str] = mapped_column(String, unique=True)
    url: Mapped[str] = mapped_column(String)
    # Whether delivery mints the Authorization bearer from a stored grant.
    # Otherwise the server authenticates through its header rows (a pasted
    # bearer is the Authorization header spelled out). A catalog-managed name
    # reads this from the registry definition instead.
    is_oauth: Mapped[bool] = mapped_column(Boolean, default=False)
    # The plain declared header values from the server's spec. A secret one,
    # like the bearer itself, is a vault row at the server's audience.
    headers: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # The first completed OAuth connect claims the credential-sharing policy.
    # A registry install or enable overlay alone carries no such decision.
    identity_mode: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    @classmethod
    async def list_all(cls, session: AsyncSession) -> list["McpServer"]:
        # The raw overlay rows — not the merged registry view (_merged).
        return list((await session.execute(select(cls).order_by(cls.name))).scalars())

    @classmethod
    async def get_for_name(cls, session: AsyncSession, name: str) -> "McpServer | None":
        return (await session.execute(select(cls).where(cls.name == name))).scalar_one_or_none()

    @classmethod
    async def _merged(cls, session: AsyncSession) -> dict[str, dict]:
        # The full view the API and delivery build from, keyed by
        # name: each built-in definition (url + auth from the registry)
        # overlaid with its operator row's enable choice and secrets, then any
        # fully custom rows. A secret is the vault row itself; its value is
        # read where it enters a run.
        rows = {server.name: server for server in await cls.list_all(session)}
        secret_headers: dict[str, dict[str, VaultSecret]] = {}
        for secret in await VaultSecret.list_installation_tokens(session):
            secret_headers.setdefault(secret.audience_name, {})[secret.header] = secret
        servers: dict[str, dict] = {}
        for definition in mcp_servers.all():
            name = definition["name"]
            row = rows.pop(name, None)
            servers[name] = {
                "name": name,
                "url": definition["url"],
                "is_oauth": definition["is_oauth"],
                "is_enabled": row.is_enabled if row else definition["enabled"],
                "headers": row.headers if row else {},
                "secret_headers": secret_headers.get(name, {}),
                "identity_mode": row.identity_mode if row else None,
                "builtin": True,
            }
        for row in rows.values():
            servers[row.name] = {
                "name": row.name,
                "url": row.url,
                "is_oauth": row.is_oauth,
                "is_enabled": row.is_enabled,
                "headers": row.headers,
                "secret_headers": secret_headers.get(row.name, {}),
                "identity_mode": row.identity_mode,
                "builtin": False,
            }
        return servers

    @classmethod
    async def list_access(
        cls, session: AsyncSession, account_id: str | None
    ) -> list[McpServerAccess]:
        default_account = await Account.get_default(session)
        is_default = bool(default_account) and default_account.id == account_id
        accesses = []
        for server in (await cls._merged(session)).values():
            service = Service.get_for_mcp_host(urlsplit(server["url"]).hostname)
            login = None
            if service and is_default:
                login = await VaultSecret.lookup(
                    session, service.secret_kind, Audience.service(service.slug)
                )
            secret = None
            if not server["is_oauth"]:
                credential = Credential.HEADERS
            elif service and service.authorization_endpoint:
                credential = Credential.SERVICE_CONNECTION
                connections = await VaultSecret.list_account_connections(
                    session, Audience.service(service.slug), account_id
                )
                secret = next(iter(connections), None)
            elif login:
                credential = Credential.SERVICE_LOGIN
                secret = login
            else:
                credential = Credential.GRANT
                if server["identity_mode"]:
                    grant_account = get_grant_account(server["identity_mode"], account_id)
                    connections = await VaultSecret.list_account_connections(
                        session, Audience.mcp(server["name"]), grant_account
                    )
                    secret = next(iter(connections), None)
            accesses.append(
                McpServerAccess(
                    **server,
                    service=service.slug if service else None,
                    credential=credential,
                    secret=secret,
                )
            )
        return accesses

    @classmethod
    async def get_access(
        cls, session: AsyncSession, name: str, account_id: str | None
    ) -> McpServerAccess | None:
        accesses = await cls.list_access(session, account_id)
        return next((access for access in accesses if access.name == name), None)

    @classmethod
    async def store_login(
        cls, session: AsyncSession, name: str, login: VaultSecret, account_id: str | None
    ) -> VaultSecret:
        """Write a service's login to the account's header row for the server: the
        issuer answers a header row verbatim, and the service's row holds only its fields."""
        service = services.get(login.audience_name)
        return await VaultSecret.store(
            session,
            SecretKind.STATIC,
            Audience.mcp(name),
            secrets={"value": service.get_authorization(login)},
            account_id=account_id,
            header=BEARER_HEADER,
        )

    @classmethod
    async def list_enabled(cls, session: AsyncSession) -> list[dict]:
        # The enabled subset — what a run delivers and the settings UI shows active.
        return [server for server in (await cls._merged(session)).values() if server["is_enabled"]]

    @classmethod
    async def set_enabled(cls, session: AsyncSession, name: str, is_enabled: bool) -> bool:
        # A built-in has no row until an operator changes its state; the enable
        # choice creates one, carrying the built-in's url. False means the name
        # is neither a row nor a catalog entry.
        server = await cls.get_for_name(session, name)
        if server:
            server.is_enabled = is_enabled
            return True
        if name in mcp_servers:
            await cls.create(
                session, name=name, url=mcp_servers.get(name)["url"], is_enabled=is_enabled
            )
            return True
        return False

    @classmethod
    async def create(
        cls,
        session: AsyncSession,
        *,
        name: str,
        url: str,
        is_oauth: bool = False,
        headers: dict[str, str] | None = None,
        secret_headers: dict[str, str] | None = None,
        is_enabled: bool = True,
    ) -> "McpServer":
        if name == DRUKS_SERVER_NAME:
            raise ReservedServerNameError(name)
        if not NAME_PATTERN.match(name):
            raise InvalidServerNameError(name)
        server = cls(
            name=name,
            url=url,
            is_oauth=is_oauth,
            headers=headers or {},
            is_enabled=is_enabled,
        )
        session.add(server)
        await session.flush()
        audience = Audience.mcp(name)
        for header, value in (secret_headers or {}).items():
            await VaultSecret.store(
                session, SecretKind.STATIC, audience, header=header, secrets={"value": value}
            )
        return server

    async def delete(self) -> None:
        for secret in await VaultSecret.list_tokens(self.session, Audience.mcp(self.name)):
            await secret.revoke("server_removed")
        await self.session.delete(self)
        await self.session.flush()
