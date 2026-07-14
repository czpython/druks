import os
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, String, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from druks.core.models import Uuid7Pk
from druks.database import db_session
from druks.extensions.registry import mcp_servers
from druks.mcp.constants import NAME_PATTERN
from druks.mcp.enums import TokenSource
from druks.mcp.exceptions import InvalidServerNameError
from druks.models import Base
from druks.secrets.fields import EncryptedJsonField, EncryptedTextField, Secret


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
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    @classmethod
    def list_all(cls) -> list["McpServer"]:
        # The raw overlay rows — not the merged registry view (get_resolved).
        return list(db_session().execute(select(cls).order_by(cls.name)).scalars())

    @classmethod
    def get_by_name(cls, name: str) -> "McpServer | None":
        return db_session().execute(select(cls).where(cls.name == name)).scalar_one_or_none()

    @classmethod
    def get_resolved(cls) -> dict[str, dict]:
        # The full view the API reads and delivery resolves from, keyed by
        # name: each built-in definition (url + auth from the registry)
        # overlaid with its operator row's enable choice and secrets, then any
        # fully custom rows.
        rows = {server.name: server for server in cls.list_all()}
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
                "builtin": False,
            }
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
                server["has_token"] = bool(McpOauthGrant.get_by_server(server["name"]))
            else:
                server["has_token"] = bool(server["token"])
        return servers

    @classmethod
    def list_enabled(cls) -> list[dict]:
        # The enabled subset — what a run delivers and the settings UI shows active.
        return [server for server in cls.get_resolved().values() if server["is_enabled"]]

    @classmethod
    def set_enabled(cls, name: str, is_enabled: bool) -> bool:
        # A built-in has no row until an operator changes its state; the enable
        # choice creates one, carrying the built-in's url. False means the name
        # is neither a row nor a catalog entry.
        server = cls.get_by_name(name)
        if server:
            server.is_enabled = is_enabled
            return True
        if name in mcp_servers:
            cls.create(name=name, url=mcp_servers.get(name)["url"], is_enabled=is_enabled)
            return True
        return False

    @classmethod
    def create(
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
        session.flush()
        return server

    def delete(self) -> None:
        session = db_session()
        session.delete(self)
        session.flush()


class McpOauthGrant(Base, Uuid7Pk):
    __tablename__ = "mcp_oauth_grants"

    # One grant per server: the durable outcome of the operator's connect flow —
    # exactly what mint needs to refresh an access token. Connect-time material
    # (authorization endpoint, PKCE verifier, state) is transient and lives in
    # Redis, never here. The refresh token never leaves the backend; the API
    # exposes only that a grant exists.
    server_name: Mapped[str] = mapped_column(String, unique=True)
    # Ciphertext at rest; decrypted only into the refresh request body.
    refresh_token = EncryptedTextField()
    token_endpoint: Mapped[str] = mapped_column(String)
    # The MCP server url the grant is bound to (RFC 8707): an audience-binding
    # authorization server rejects a refresh that doesn't carry the same
    # ``resource`` the code exchange did.
    resource: Mapped[str] = mapped_column(String)
    client_id: Mapped[str] = mapped_column(String)
    # "" for public clients (PKCE-only); some authorization servers issue one
    # even for token_endpoint_auth_method "none" and then expect it on refresh.
    client_secret = EncryptedTextField(default="")
    # When the operator last completed consent. Stamped on every store — the
    # row is upserted on re-connect, so row-creation time would lie.
    connected_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    @classmethod
    def get_by_server(cls, server_name: str) -> "McpOauthGrant | None":
        return (
            db_session()
            .execute(select(cls).where(cls.server_name == server_name))
            .scalar_one_or_none()
        )

    @classmethod
    def store(
        cls,
        *,
        server_name: str,
        refresh_token: str,
        token_endpoint: str,
        resource: str,
        client_id: str,
        client_secret: str = "",
    ) -> "McpOauthGrant":
        # Connecting again replaces the grant — the recovery path for a revoked
        # or rotten refresh token.
        session = db_session()
        grant = cls.get_by_server(server_name)
        if not grant:
            grant = cls(server_name=server_name)
            session.add(grant)
        grant.refresh_token = refresh_token
        grant.token_endpoint = token_endpoint
        grant.resource = resource
        grant.client_id = client_id
        grant.client_secret = client_secret
        grant.connected_at = cls.utc_now()
        session.flush()
        return grant

    def delete(self) -> None:
        session = db_session()
        session.delete(self)
        session.flush()
