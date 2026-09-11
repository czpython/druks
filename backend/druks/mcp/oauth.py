import json
import logging
import secrets
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from authlib.oidc.core import CodeIDToken
from joserfc import jws
from joserfc.errors import JoseError
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from druks.database import db_session
from druks.mcp.constants import OAUTH_CALLBACK_PATH
from druks.mcp.enums import IdentityMode
from druks.mcp.exceptions import (
    GrantRefreshError,
    IdentityLookupError,
    MissingGrantError,
    OauthConnectError,
)
from druks.mcp.helpers import get_grant_account
from druks.mcp.models import McpServer
from druks.secrets.datastructures import Audience
from druks.secrets.enums import IdentityStatus
from druks.secrets.models import VaultSecret
from druks.services import OauthClient, OauthExchangeError, OauthRefreshError
from druks.services.constants import OAUTH_MINT_WAIT_ATTEMPTS, OAUTH_MINT_WAIT_INTERVAL_SECONDS
from druks.services.oauth import complete_connect as complete_oauth_exchange

logger = logging.getLogger(__name__)


def _http() -> httpx.AsyncClient:
    # One construction point so the suite can swap in a MockTransport client.
    return httpx.AsyncClient(timeout=30.0, follow_redirects=True)


async def get_connection(name: str, account_id: str | None) -> VaultSecret | None:
    # One live grant per (server, account) — MCP's policy over the vault.
    # Revoked rows stay behind as history.
    rows = await VaultSecret.list_account_connections(Audience.mcp(name), account_id)
    return rows[0] if rows else None


async def list_connections(name: str) -> list[VaultSecret]:
    return await VaultSecret.list_connections(Audience.mcp(name))


def _origin(url: str) -> str:
    parts = urlparse(url)
    return f"{parts.scheme}://{parts.netloc}"


def _same_origin_userinfo(metadata: dict) -> str:
    # The token goes here as a bearer at consent, so only trust a userinfo
    # endpoint on the issuer's own origin — off-issuer would exfiltrate it.
    endpoint = metadata.get("userinfo_endpoint", "")
    if not endpoint or _origin(endpoint) == _origin(metadata["issuer"]):
        return endpoint
    logger.warning(
        "ignoring userinfo_endpoint %s off the issuer origin %s", endpoint, metadata["issuer"]
    )
    return ""


async def _get_json(client: httpx.AsyncClient, url: str) -> dict | None:
    # A discovery probe: any failure — network, non-2xx, non-JSON, non-object —
    # just means this candidate url isn't it.
    try:
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return
    return payload if isinstance(payload, dict) else None


async def _discover(
    client: httpx.AsyncClient, name: str, server_url: str
) -> tuple[dict, dict, list[str]]:
    """The OAuth metadata, OpenID metadata, and resource scopes of an MCP server."""
    origin = _origin(server_url)
    path = urlparse(server_url).path.rstrip("/")
    issuer = ""
    resource_scopes = []
    for well_known in (
        f"{origin}/.well-known/oauth-protected-resource{path}",
        f"{origin}/.well-known/oauth-protected-resource",
    ):
        resource_metadata = await _get_json(client, well_known)
        if resource_metadata and resource_metadata.get("authorization_servers"):
            claimed = resource_metadata.get("resource", "")
            if claimed.rstrip("/") != server_url.rstrip("/"):
                raise OauthConnectError(
                    name,
                    f"protected-resource metadata at {well_known} is about "
                    f"{claimed!r}, not {server_url!r}",
                )
            issuer = resource_metadata["authorization_servers"][0]
            resource_scopes = resource_metadata.get("scopes_supported", [])
            break
    if issuer:
        candidate_issuers = [issuer]
    else:
        # No protected-resource metadata: the server is its own issuer, or its
        # origin document names another — Atlassian serves its CDN alias this
        # way — a claim with the same trust RFC 9728 gives authorization_servers.
        document = await _get_json(client, f"{origin}/.well-known/oauth-authorization-server")
        alias = document.get("issuer") if document else None
        candidate_issuers = [origin, alias] if alias and alias != origin else [origin]
    for issuer in candidate_issuers:
        if metadata := await _issuer_metadata(client, issuer):
            return *metadata, resource_scopes
    raise OauthConnectError(
        name,
        f"no authorization-server metadata claiming issuer {issuer} found for {server_url}",
    )


async def _claimed_metadata(client: httpx.AsyncClient, issuer: str, *urls: str) -> dict | None:
    """The first document that claims the issuer and names both endpoints. A
    document for another issuer is never trusted (RFC 8414 section 3.3)."""
    for url in dict.fromkeys(urls):
        metadata = await _get_json(client, url)
        if (
            metadata
            and metadata.get("issuer") == issuer
            and metadata.get("authorization_endpoint")
            and metadata.get("token_endpoint")
        ):
            return metadata


async def _issuer_metadata(client: httpx.AsyncClient, issuer: str) -> tuple[dict, dict] | None:
    """The issuer's OAuth metadata, and the OpenID metadata that speaks for it."""
    origin = _origin(issuer)
    path = urlparse(issuer).path.rstrip("/")
    openid = await _claimed_metadata(
        client,
        issuer,
        f"{origin}/.well-known/openid-configuration{path}",
        f"{origin}{path}/.well-known/openid-configuration",
    )
    oauth = (
        await _claimed_metadata(
            client,
            issuer,
            f"{origin}/.well-known/oauth-authorization-server{path}",
            f"{origin}/.well-known/oauth-authorization-server",
        )
        or openid
    )
    if oauth:
        root = await _claimed_metadata(client, origin, f"{origin}/.well-known/openid-configuration")
        # The token endpoint issues the ID token, so a provider with the same
        # endpoints speaks for the same authority.
        for identity in (openid, root):
            if identity and all(
                identity[field] == oauth[field]
                for field in ("authorization_endpoint", "token_endpoint")
            ):
                return oauth, identity
        return oauth, oauth


async def _register_client(
    client: httpx.AsyncClient,
    name: str,
    metadata: dict,
    redirect_uri: str,
    scopes: tuple[str, ...],
) -> dict:
    # RFC 7591 dynamic registration of a public client (PKCE, no client auth).
    # A server without a registration endpoint needs a configured client id —
    # unsupported until such a server exists.
    registration_endpoint = metadata.get("registration_endpoint", "")
    if not registration_endpoint:
        raise OauthConnectError(
            name, "the authorization server does not support dynamic client registration"
        )
    client_metadata = {
        "client_name": "druks",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    if scopes:
        # A provider can limit a client to the scopes it registered and refuse
        # any other scope at consent.
        client_metadata["scope"] = " ".join(scopes)
    try:
        response = await client.post(registration_endpoint, json=client_metadata)
    except httpx.HTTPError as error:
        raise OauthConnectError(name, f"client registration failed: {error}") from error
    if response.status_code not in (200, 201):
        raise OauthConnectError(name, f"client registration failed: HTTP {response.status_code}")
    try:
        registration = response.json()
    except ValueError as error:
        raise OauthConnectError(name, "client registration returned malformed JSON") from error
    if not isinstance(registration, dict) or not registration.get("client_id"):
        raise OauthConnectError(name, "client registration returned no client_id")
    return registration


def _supported_scopes(name: str, scopes: object) -> list[str]:
    if isinstance(scopes, list) and all(isinstance(scope, str) for scope in scopes):
        return scopes
    raise OauthConnectError(name, "the server returned invalid supported scopes")


async def begin_connect(
    name: str,
    server_url: str,
    endpoint: str,
    *,
    account_id: str | None,
    identity_mode: IdentityMode,
) -> str:
    """Start the operator's authorization-code + PKCE flow for one server:
    discover the authorization server, register druks as a public client, and
    hand the engine the resulting client to stash the pending exchange and
    render the consent URL. Nothing durable is written here — an abandoned
    consent simply expires."""
    redirect_uri = f"{endpoint.rstrip('/')}{OAUTH_CALLBACK_PATH}"
    async with _http() as client:
        metadata, identity_metadata, resource_scopes = await _discover(client, name, server_url)
        # Absent means the OAuth 2.1 baseline (S256); advertised-without-S256
        # means the flow below cannot work — fail before the consent screen.
        methods = metadata.get("code_challenge_methods_supported")
        if methods is not None and "S256" not in methods:
            raise OauthConnectError(name, "the authorization server does not support PKCE S256")
        supported_scopes = _supported_scopes(name, identity_metadata.get("scopes_supported", []))
        scopes = ()
        if "openid" in supported_scopes:
            # A scope request replaces the provider's default grant, so it must
            # also ask for the MCP resource's scopes.
            identity_scopes = [
                scope for scope in ("openid", "email", "profile") if scope in supported_scopes
            ]
            scopes = tuple(
                dict.fromkeys([*_supported_scopes(name, resource_scopes), *identity_scopes])
            )
        registration = await _register_client(client, name, metadata, redirect_uri, scopes)
    nonce = secrets.token_urlsafe(32)
    return await OauthClient(
        provider=Audience.mcp(name),
        authorization_endpoint=metadata["authorization_endpoint"],
        token_endpoint=metadata["token_endpoint"],
        client_id=registration["client_id"],
        client_secret=registration.get("client_secret", ""),
        # RFC 8707: bind the tokens to the MCP server they are for.
        extra_token_params={"resource": server_url},
    ).begin_connect(
        redirect_uri=redirect_uri,
        scopes=scopes,
        context={
            "name": name,
            "server_url": server_url,
            "account_id": account_id,
            "identity_mode": identity_mode,
            "userinfo_endpoint": _same_origin_userinfo(identity_metadata),
            "issuer": identity_metadata["issuer"],
            "nonce": nonce,
        },
        extra_authorize_params={"resource": server_url, "nonce": nonce},
    )


async def get_grant_identity(tokens: dict, pending: dict) -> tuple[dict, IdentityStatus]:
    """The grant's identity and the lookup outcome. A failed lookup keeps the grant."""
    status = IdentityStatus.UNAVAILABLE
    for source in ("id_token", "userinfo"):
        try:
            if source == "id_token" and isinstance(tokens.get("id_token"), str):
                claims = read_id_token(tokens, pending)
            elif source == "userinfo" and pending["userinfo_endpoint"]:
                async with _http() as client:
                    response = await client.get(
                        pending["userinfo_endpoint"],
                        headers={"Authorization": f"Bearer {tokens['access_token']}"},
                        follow_redirects=False,
                    )
                    response.raise_for_status()
                    claims = response.json()
            else:
                continue
            identity = get_identity_facts(claims, authority=pending["issuer"], source=source)
            return identity, IdentityStatus.RESOLVED
        except (
            IdentityLookupError,
            JoseError,
            httpx.HTTPError,
            httpx.InvalidURL,
            ValueError,
        ) as error:
            # These errors name the failed check, never the token.
            logger.warning(
                "MCP identity lookup failed for %s via %s: %r", pending["name"], source, error
            )
            status = IdentityStatus.FAILED
    return {}, status


def read_id_token(tokens: dict, pending: dict) -> dict:
    """The ID token's claims, checked for the code flow. TLS to the token
    endpoint stands in for the signature check (OpenID Connect Core 3.1.3.7)."""
    token = jws.extract_compact(tokens["id_token"].encode())
    payload = json.loads(token.payload)
    if not isinstance(payload, dict):
        raise IdentityLookupError("The ID token payload is not a JSON object.")
    claims = CodeIDToken(
        payload,
        token.headers(),
        options={
            "iss": {"value": pending["issuer"]},
            "aud": {"value": pending["client_id"]},
        },
        params={
            "client_id": pending["client_id"],
            "nonce": pending["nonce"],
            "access_token": tokens["access_token"],
        },
    )
    claims.validate(leeway=30)
    return dict(claims)


def get_identity_facts(payload: Any, *, authority: str, source: str) -> dict[str, Any]:
    """A nonblank OpenID subject establishes identity. Profile fields are optional."""
    if not isinstance(payload, dict):
        raise IdentityLookupError("Identity response must be a JSON object.")
    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise IdentityLookupError("Identity response has no stable subject.")
    identity = {"authority": authority, "subject": subject, "source": source}
    for field in ("name", "email"):
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            identity[field] = value
    if "email" in identity and isinstance(payload.get("email_verified"), bool):
        identity["email_verified"] = payload["email_verified"]
    return identity


async def complete_connect(*, state: str, code: str) -> str:
    """The callback half: the shared exchange, then the durable outcome —
    claim the server's identity mode and store the registration and the
    grant. Returns the server name."""
    try:
        tokens, pending = await complete_oauth_exchange(state=state, code=code)
    except OauthExchangeError as error:
        raise OauthConnectError(error.context.get("name", "unknown"), error.reason) from error
    name = pending["name"]
    identity, identity_status = await get_grant_identity(tokens, pending)
    # An omitted scope means the provider granted the requested scopes (RFC 6749 section 5.1).
    scope = tokens.get("scope", " ".join(pending["scopes"]) or None)
    scopes = scope.split() if isinstance(scope, str) else None
    # The first completed connect claims the mode: insert the row if absent,
    # fill the mode if unclaimed. A concurrent claim wins the row lock; the
    # select reads whichever choice landed, and the grant goes under it.
    session = db_session()
    await session.execute(
        pg_insert(McpServer)
        .values(
            name=name,
            url=pending["server_url"],
            identity_mode=pending["identity_mode"],
        )
        .on_conflict_do_nothing(index_elements=["name"])
    )
    await session.execute(
        update(McpServer)
        .where(McpServer.name == name, McpServer.identity_mode.is_(None))
        .values(identity_mode=pending["identity_mode"])
    )
    server = (await session.scalars(select(McpServer).where(McpServer.name == name))).one()
    account_id = get_grant_account(server.identity_mode, pending["account_id"])
    # Druks registered a fresh client for this consent; the grant refreshes
    # through it, so the client rides in the grant's secrets.
    client = {
        "token_endpoint": pending["token_endpoint"],
        "client_id": pending["client_id"],
        "client_secret": pending["client_secret"],
    }
    connection = await get_connection(name, account_id)
    if connection:
        await connection.reconnect(
            refresh_token=tokens["refresh_token"],
            scopes=scopes,
            identity=identity,
            identity_status=identity_status,
            secrets=client,
        )
        # A reconsent's stale cached token must not serve until its TTL runs out.
        await evict_access_token(name, account_id)
    else:
        await VaultSecret.connect(
            Audience.mcp(name),
            account_id=account_id,
            refresh_token=tokens["refresh_token"],
            scopes=scopes,
            identity=identity,
            identity_status=identity_status,
            secrets=client,
        )
    return name


async def evict_access_token(name: str, account_id: str | None) -> None:
    connection = await get_connection(name, account_id)
    if connection:
        await OauthClient(provider=Audience.mcp(name)).evict_access_token(connection.id)


async def disconnect(name: str, account_id: str | None, *, reason: str = "user") -> None:
    # The grant's secrets carry its client, so one revoke ends both.
    connection = await get_connection(name, account_id)
    if connection:
        await OauthClient(provider=Audience.mcp(name)).disconnect(connection, reason=reason)


async def get_access_token(name: str, account_id: str | None) -> tuple[str, datetime | None]:
    """The token for a connected server and its expiry, served by the shared
    engine from this server's grant — the issuer never answers a server the
    agent can't authenticate to."""
    connection = await get_connection(name, account_id)
    server = await McpServer.get_for_name(name)
    if not connection or not server or "client_id" not in connection.secrets:
        raise MissingGrantError(name, account_id)
    client = OauthClient(
        provider=Audience.mcp(name),
        token_endpoint=connection.secrets["token_endpoint"],
        client_id=connection.secrets["client_id"],
        client_secret=connection.secrets.get("client_secret", ""),
        # RFC 8707: an audience-binding server expects the refresh to carry
        # the same resource the code exchange was bound to.
        extra_token_params={"resource": server.url},
        mint_wait_interval_seconds=OAUTH_MINT_WAIT_INTERVAL_SECONDS,
        mint_wait_attempts=OAUTH_MINT_WAIT_ATTEMPTS,
    )
    try:
        return await client.get_access_token(connection=connection)
    except OauthRefreshError as error:
        raise GrantRefreshError(name, error.reason) from error
