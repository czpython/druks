import asyncio
import base64
import hashlib
import json
import secrets
from urllib.parse import urlencode, urlparse

import httpx

from druks.database import db_session
from druks.mcp.constants import (
    OAUTH_ACCESS_TOKEN_PREFIX,
    OAUTH_CALLBACK_PATH,
    OAUTH_CONNECT_STATE_PREFIX,
    OAUTH_CONNECT_STATE_TTL_SECONDS,
    OAUTH_MINT_WAIT_ATTEMPTS,
    OAUTH_MINT_WAIT_INTERVAL_SECONDS,
    OAUTH_REFRESH_LOCK_PREFIX,
    OAUTH_REFRESH_LOCK_TTL_SECONDS,
    OAUTH_TOKEN_TTL_SKEW_SECONDS,
)
from druks.mcp.exceptions import GrantRefreshError, MissingGrantError, OauthConnectError
from druks.mcp.models import McpOauthGrant
from druks.redis import get_client


def _http() -> httpx.AsyncClient:
    # One construction point so the suite can swap in a MockTransport client.
    return httpx.AsyncClient(timeout=30.0, follow_redirects=True)


def _origin(url: str) -> str:
    parts = urlparse(url)
    return f"{parts.scheme}://{parts.netloc}"


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


async def _discover(client: httpx.AsyncClient, name: str, server_url: str) -> dict:
    """The authorization-server metadata for an MCP server: RFC 9728
    protected-resource metadata names the authorization server, RFC 8414 (or
    OIDC) metadata names its endpoints. Well-known lookups try the path-aware
    forms first, then the root; a server predating RFC 9728 falls back to
    being its own authorization server. Both documents are verified to be
    about what we asked for — the resource must echo the server url and the
    issuer must claim itself (the RFC 9728 §3.3 / 8414 §3.3 mix-up defenses);
    a document claiming another issuer is skipped, never trusted."""
    origin = _origin(server_url)
    path = urlparse(server_url).path.rstrip("/")
    issuer = ""
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
            break
    if not issuer:
        issuer = origin
    issuer_origin = _origin(issuer)
    issuer_path = urlparse(issuer).path.rstrip("/")
    # An issuer with a path publishes under RFC 8414 insertion
    # (/.well-known/...{path}) or OIDC insertion ({path}/.well-known/...);
    # dict.fromkeys collapses the duplicates a pathless issuer produces.
    candidates = dict.fromkeys(
        (
            f"{issuer_origin}/.well-known/oauth-authorization-server{issuer_path}",
            f"{issuer_origin}/.well-known/oauth-authorization-server",
            f"{issuer_origin}/.well-known/openid-configuration{issuer_path}",
            f"{issuer_origin}{issuer_path}/.well-known/openid-configuration",
            f"{issuer_origin}/.well-known/openid-configuration",
        )
    )
    for well_known in candidates:
        metadata = await _get_json(client, well_known)
        if (
            metadata
            and metadata.get("authorization_endpoint")
            and metadata.get("token_endpoint")
            and metadata.get("issuer", "").rstrip("/") == issuer.rstrip("/")
        ):
            return metadata
    raise OauthConnectError(
        name,
        f"no authorization-server metadata claiming issuer {issuer} found for {server_url}",
    )


async def _register_client(
    client: httpx.AsyncClient, name: str, metadata: dict, redirect_uri: str
) -> dict:
    # RFC 7591 dynamic registration of a public client (PKCE, no client auth).
    # A server without a registration endpoint needs a configured client id —
    # unsupported until such a server exists.
    registration_endpoint = metadata.get("registration_endpoint", "")
    if not registration_endpoint:
        raise OauthConnectError(
            name, "the authorization server does not support dynamic client registration"
        )
    try:
        response = await client.post(
            registration_endpoint,
            json={
                "client_name": "druks",
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
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


async def begin_connect(name: str, server_url: str, endpoint: str) -> str:
    """Start the operator's authorization-code + PKCE flow for one server:
    discover the authorization server, register druks as a public client, stash
    the pending exchange (verifier + endpoints) in Redis under the state, and
    return the consent URL to open. Nothing durable is written here — an
    abandoned consent simply expires."""
    redirect_uri = f"{endpoint.rstrip('/')}{OAUTH_CALLBACK_PATH}"
    async with _http() as client:
        metadata = await _discover(client, name, server_url)
        # Absent means the OAuth 2.1 baseline (S256); advertised-without-S256
        # means the flow below cannot work — fail before the consent screen.
        methods = metadata.get("code_challenge_methods_supported")
        if methods is not None and "S256" not in methods:
            raise OauthConnectError(name, "the authorization server does not support PKCE S256")
        registration = await _register_client(client, name, metadata, redirect_uri)
    state = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    pending = {
        "name": name,
        "server_url": server_url,
        "code_verifier": code_verifier,
        "token_endpoint": metadata["token_endpoint"],
        "client_id": registration["client_id"],
        "client_secret": registration.get("client_secret", ""),
        "redirect_uri": redirect_uri,
    }
    await get_client().set(
        f"{OAUTH_CONNECT_STATE_PREFIX}{state}",
        json.dumps(pending),
        ex=OAUTH_CONNECT_STATE_TTL_SECONDS,
    )
    query = urlencode(
        {
            "response_type": "code",
            "client_id": registration["client_id"],
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            # RFC 8707: bind the token to the MCP server it is for.
            "resource": server_url,
        }
    )
    return f"{metadata['authorization_endpoint']}?{query}"


async def complete_connect(*, state: str, code: str) -> str:
    """The callback half: consume the pending state (single-use), exchange the
    code + verifier for tokens, and store the grant. Returns the server name.
    The grant is the only outcome — nothing is cached here, because it becomes
    real only when this request's transaction commits, and a cache filled
    ahead of that would outlive its failure. The first delivery mints from the
    committed grant."""
    raw = await get_client().getdel(f"{OAUTH_CONNECT_STATE_PREFIX}{state}")
    if not raw:
        raise OauthConnectError("unknown", "unknown or expired state; start the connect flow again")
    pending = json.loads(raw)
    name = pending["name"]
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": pending["redirect_uri"],
        "client_id": pending["client_id"],
        "code_verifier": pending["code_verifier"],
        "resource": pending["server_url"],
    }
    if pending["client_secret"]:
        data["client_secret"] = pending["client_secret"]
    async with _http() as client:
        try:
            response = await client.post(pending["token_endpoint"], data=data)
        except httpx.HTTPError as error:
            raise OauthConnectError(name, f"code exchange failed: {error}") from error
    if response.status_code != 200:
        raise OauthConnectError(name, f"code exchange failed: HTTP {response.status_code}")
    try:
        tokens = response.json()
    except ValueError as error:
        raise OauthConnectError(name, "the token endpoint returned malformed JSON") from error
    if not isinstance(tokens, dict) or not tokens.get("refresh_token"):
        raise OauthConnectError(
            name, "the authorization server granted no refresh token; druks needs offline access"
        )
    McpOauthGrant.store(
        server_name=name,
        refresh_token=tokens["refresh_token"],
        token_endpoint=pending["token_endpoint"],
        resource=pending["server_url"],
        client_id=pending["client_id"],
        client_secret=pending["client_secret"],
    )
    return name


async def evict_access_token(name: str) -> None:
    await get_client().delete(f"{OAUTH_ACCESS_TOKEN_PREFIX}{name}")


async def mint_access_token(name: str) -> str:
    """The delivery-side token for a connected server: the cached access token
    while it lives, else one refreshed from the grant. The provider may rotate
    the refresh token on use — two concurrent refreshes trip its reuse
    detection and can revoke the whole grant — so the Redis that fronts the
    cache also elects one refresher per server (the run lock's SET NX idiom;
    the TTL is a crash backstop a live refresh cannot outlive). Losers poll
    for the winner's cache fill, for about one token-endpoint round trip, then
    fail loudly — delivery never ships a server the agent can't authenticate
    to."""
    redis = get_client()
    token_key = f"{OAUTH_ACCESS_TOKEN_PREFIX}{name}"
    lock_key = f"{OAUTH_REFRESH_LOCK_PREFIX}{name}"
    for _ in range(OAUTH_MINT_WAIT_ATTEMPTS):
        cached = await redis.get(token_key)
        if cached:
            return cached.decode()
        if await redis.set(lock_key, "1", nx=True, ex=OAUTH_REFRESH_LOCK_TTL_SECONDS):
            break
        await asyncio.sleep(OAUTH_MINT_WAIT_INTERVAL_SECONDS)
    else:
        raise GrantRefreshError(name, "timed out waiting for a concurrent refresh to finish")
    try:
        grant = McpOauthGrant.get_for_server(name)
        if not grant:
            raise MissingGrantError(name)
        # The grant's secret halves are ciphertext at rest; the plaintext
        # exists only in this request body.
        data = {
            "grant_type": "refresh_token",
            "refresh_token": grant.refresh_token.decrypt(),
            "client_id": grant.client_id,
            # RFC 8707: an audience-binding server expects the refresh to carry
            # the same resource the code exchange was bound to.
            "resource": grant.resource,
        }
        if grant.client_secret:
            data["client_secret"] = grant.client_secret.decrypt()
        async with _http() as client:
            try:
                response = await client.post(grant.token_endpoint, data=data)
            except httpx.HTTPError as error:
                raise GrantRefreshError(name, str(error)) from error
        if response.status_code != 200:
            await evict_access_token(name)
            raise GrantRefreshError(name, f"HTTP {response.status_code} from the token endpoint")
        try:
            tokens = response.json()
        except ValueError as error:
            raise GrantRefreshError(name, "the token endpoint returned malformed JSON") from error
        if not isinstance(tokens, dict) or not tokens.get("access_token"):
            raise GrantRefreshError(name, "the token endpoint returned no access token")
        if tokens.get("refresh_token"):
            # Rotation: the provider invalidated the old refresh token on use.
            # The write rides the enclosing transaction, so until its commit a
            # crash loses it and a concurrent minter in another session still
            # reads the spent token (only reachable when the provider's
            # expires_in undercuts the run's remaining duration — the cache
            # covers the window otherwise). Either way the next mint fails
            # loudly and re-connecting replaces the grant; that recovery path
            # is the accepted cost of not committing mid-step.
            grant.refresh_token = tokens["refresh_token"]
            db_session().flush()
        try:
            ttl = int(tokens.get("expires_in", 3600)) - OAUTH_TOKEN_TTL_SKEW_SECONDS
        except (TypeError, ValueError) as error:
            raise GrantRefreshError(
                name, "the token endpoint returned a malformed expires_in"
            ) from error
        if ttl > 0:
            await redis.set(token_key, tokens["access_token"], ex=ttl)
        return tokens["access_token"]
    finally:
        await redis.delete(lock_key)
