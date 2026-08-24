import asyncio
import base64
import hashlib
import json
import secrets
from typing import Any, cast
from urllib.parse import urlencode

import httpx

from druks.redis import get_client

from .constants import (
    OAUTH_CONNECT_STATE_TTL_SECONDS,
    OAUTH_MINT_WAIT_ATTEMPTS,
    OAUTH_MINT_WAIT_INTERVAL_SECONDS,
    OAUTH_REFRESH_LOCK_TTL_SECONDS,
    OAUTH_TOKEN_TTL_SKEW_SECONDS,
)
from .exceptions import OauthExchangeError, OauthRefreshError
from .models import OauthConnection


def _http() -> httpx.AsyncClient:
    # One construction point so a suite can swap in a MockTransport client.
    return httpx.AsyncClient(timeout=30.0, follow_redirects=True)


async def _post_token(
    http: httpx.AsyncClient,
    token_endpoint: str,
    data: dict[str, Any],
    *,
    client_id: str,
    client_secret: str,
    basic_auth: bool,
) -> httpx.Response:
    # RFC 6749 client authentication: HTTP Basic keeps the credentials out of
    # the form body; a public or body-authenticating client sends them in it.
    if basic_auth:
        return await http.post(token_endpoint, data=data, auth=(client_id, client_secret))
    data["client_id"] = client_id
    if client_secret:
        data["client_secret"] = client_secret
    return await http.post(token_endpoint, data=data)


class OauthClient:
    """One provider's OAuth 2.0 authorization-code + PKCE flow, with
    rotation-safe refresh — for a provider with fixed endpoints and a
    pre-registered client::

        client = OauthClient(
            provider="acme",
            authorization_endpoint="https://acme.example/oauth/authorize",
            token_endpoint="https://acme.example/oauth/token",
            client_id=..., client_secret=...,
            basic_auth=True,
        )

    ``begin_connect`` returns the consent URL to open; the module-level
    ``complete_connect`` consumes the callback's single-use state and
    exchanges the code; ``get_access_token`` serves delivery from the Redis
    token cache, electing one refresher per connection. The caller stores an
    ``OauthConnection`` from the completed exchange and hands it back to
    ``get_access_token``. A ``Service`` with declared OAuth endpoints hands back a
    configured client via ``get_oauth_client()`` — construct directly only
    when no service holds the client credentials.

    The Redis token cache and refresh lock key on the connection id, so all
    clients constructed for one provider share them, and across a rolling
    deploy old and new processes elect the same single refresher. Connect
    state is keyed by the state value alone: the begun flow's provider,
    endpoints, and client identity ride the stash, pinned at begin time so a
    configuration change mid-consent cannot mismatch the PKCE verifier.

    ``basic_auth`` picks HTTP Basic on the token endpoint, for both the code
    exchange and refresh; without it the client credentials travel in the form
    body. ``extra_token_params`` land in both bodies (RFC 8707's ``resource``
    audience binding); ``extra_authorize_params`` land in every consent query
    (Google grants a refresh token only with ``access_type=offline`` and
    ``prompt=consent``). Scopes are per authorization, not per client — each
    ``begin_connect`` asks for its own, and the grant keeps what was approved.
    """

    def __init__(
        self,
        *,
        provider: str,
        authorization_endpoint: str = "",
        token_endpoint: str = "",
        client_id: str = "",
        client_secret: str = "",
        basic_auth: bool = False,
        extra_token_params: dict[str, str] | None = None,
        extra_authorize_params: dict[str, str] | None = None,
        mint_wait_interval_seconds: float = OAUTH_MINT_WAIT_INTERVAL_SECONDS,
        mint_wait_attempts: int = OAUTH_MINT_WAIT_ATTEMPTS,
    ) -> None:
        self.provider = provider
        self.authorization_endpoint = authorization_endpoint
        self.token_endpoint = token_endpoint
        self.client_id = client_id
        self.client_secret = client_secret
        self.basic_auth = basic_auth
        self.extra_token_params = dict(extra_token_params or {})
        self.extra_authorize_params = dict(extra_authorize_params or {})
        self.mint_wait_interval_seconds = mint_wait_interval_seconds
        self.mint_wait_attempts = mint_wait_attempts

    async def begin_connect(
        self,
        *,
        redirect_uri: str,
        scopes: tuple[str, ...] = (),
        context: dict[str, Any] | None = None,
        extra_authorize_params: dict[str, str] | None = None,
    ) -> str:
        """Stash the pending exchange in Redis under a fresh single-use state
        and return the consent URL to open. ``scopes`` render into the consent
        query — this authorization's ask, within whatever ceiling the provider
        registration allows. ``context`` rides the stash and comes back from
        ``complete_connect``; ``extra_authorize_params`` land in the consent
        query, over the client's declared ones on a shared key. Nothing durable
        is written here — an abandoned consent simply expires."""
        state = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        pending = {
            **(context or {}),
            "provider": self.provider,
            "scopes": list(scopes),
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
            "token_endpoint": self.token_endpoint,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "basic_auth": self.basic_auth,
            "extra_token_params": self.extra_token_params,
        }
        await get_client().set(
            f"oauth:connect:{state}",
            json.dumps(pending),
            ex=OAUTH_CONNECT_STATE_TTL_SECONDS,
        )
        query = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        if scopes:
            query["scope"] = " ".join(scopes)
        query.update({**self.extra_authorize_params, **(extra_authorize_params or {})})
        return f"{self.authorization_endpoint}?{urlencode(query)}"

    async def get_access_token(
        self,
        *,
        connection: OauthConnection,
        scopes: tuple[str, ...] = (),
        cached: bool = True,
    ) -> str:
        """The delivery-side token for one connection: the cached access
        token while it lives, else one refreshed through the stored refresh
        token. The provider may rotate the refresh token on use — two
        concurrent refreshes trip its reuse detection and can revoke the
        whole connection — so Redis elects one refresher per (connection,
        scope set) (SET NX; the TTL is a crash backstop a live refresh cannot
        outlive). Losers poll for the winner's cache fill, for about one
        token-endpoint round trip, then fail loudly. The engine reads the
        connection fresh under the lock and commits a rotated token before
        the cache fills.

        ``scopes`` asks the provider for a token narrower than the grant
        (RFC 6749 §6) — a server-side ceiling for a token handed to
        untrusted compute; it must be a subset of the connection's granted
        scopes. ``cached=False`` skips the cache read for a full-lifetime
        token, still electing one refresher and filling the cache for later
        callers."""
        if connection.revoked_at:
            raise OauthRefreshError(
                self.provider, "the connection is revoked; sign in again to restore it"
            )
        requested = tuple(sorted(scopes))
        if requested and not set(requested) <= set(connection.scopes):
            missing = ", ".join(sorted(set(requested) - set(connection.scopes)))
            raise OauthRefreshError(
                self.provider, f"the connection does not grant scope(s) {missing}"
            )
        redis = get_client()
        # A down-scoped token must never serve a full-scope caller, or the
        # reverse — the cache and the refresher election key on the scope set.
        suffix = ""
        if requested:
            suffix = ":" + hashlib.sha256(" ".join(requested).encode()).hexdigest()[:16]
        token_key = f"{self.provider}:access_token:{connection.id}{suffix}"
        lock_key = f"{self.provider}:refresh_lock:{connection.id}{suffix}"
        for _ in range(self.mint_wait_attempts):
            if cached:
                cached_token = await redis.get(token_key)
                if cached_token:
                    return cast(bytes, cached_token).decode()
            if await redis.set(lock_key, "1", nx=True, ex=OAUTH_REFRESH_LOCK_TTL_SECONDS):
                break
            await asyncio.sleep(self.mint_wait_interval_seconds)
        else:
            raise OauthRefreshError(
                self.provider, "timed out waiting for a concurrent refresh to finish"
            )
        try:
            data = {
                "grant_type": "refresh_token",
                "refresh_token": await connection._load_refresh_token(),
                **self.extra_token_params,
            }
            if requested:
                data["scope"] = " ".join(requested)
            async with _http() as http:
                try:
                    response = await _post_token(
                        http,
                        self.token_endpoint,
                        data,
                        client_id=self.client_id,
                        client_secret=self.client_secret,
                        basic_auth=self.basic_auth,
                    )
                except httpx.HTTPError as error:
                    raise OauthRefreshError(self.provider, str(error)) from error
            if response.status_code != 200:
                await redis.delete(token_key)
                raise OauthRefreshError(
                    self.provider, f"HTTP {response.status_code} from the token endpoint"
                )
            try:
                tokens = response.json()
            except ValueError as error:
                raise OauthRefreshError(
                    self.provider, "the token endpoint returned malformed JSON"
                ) from error
            if not isinstance(tokens, dict) or not tokens.get("access_token"):
                raise OauthRefreshError(
                    self.provider, "the token endpoint returned no access token"
                )
            if tokens.get("refresh_token"):
                await connection._save_refresh_token(tokens["refresh_token"])
            if requested and tokens.get("scope") and set(tokens["scope"].split()) != set(requested):
                # A provider that ignores the narrowing hands back a token the
                # sandbox must never hold — fail rather than cache it.
                raise OauthRefreshError(
                    self.provider,
                    f"asked for scope(s) {' '.join(requested)}; "
                    f"the token came back with {tokens['scope']!r}",
                )
            try:
                ttl = int(tokens.get("expires_in", 3600)) - OAUTH_TOKEN_TTL_SKEW_SECONDS
            except (TypeError, ValueError) as error:
                raise OauthRefreshError(
                    self.provider, "the token endpoint returned a malformed expires_in"
                ) from error
            if ttl > 0:
                await redis.set(token_key, tokens["access_token"], ex=ttl)
            return tokens["access_token"]
        finally:
            await redis.delete(lock_key)

    async def evict_access_token(self, connection_id: str) -> None:
        # Down-scoped variants ride the same prefix; one sweep drops them all.
        redis = get_client()
        async for key in redis.scan_iter(match=f"{self.provider}:access_token:{connection_id}*"):
            await redis.delete(key)

    async def disconnect(self, connection: OauthConnection, *, reason: str) -> None:
        """Revoke the connection and evict its cached access token. The row
        and its facts survive; the refresh token dies with the consent."""
        await connection.revoke(reason)
        await self.evict_access_token(connection.id)


async def complete_connect(*, state: str, code: str) -> tuple[dict, dict]:
    """Consume the pending state (single-use, GETDEL) and exchange the code
    for tokens; a callback route knows only ``state`` and ``code``, so the
    flow's provider and client identity ride the stash. Returns ``(tokens,
    pending)`` — the caller stores the grant, because only it knows the
    grant's account."""
    raw = await get_client().getdel(f"oauth:connect:{state}")
    if not raw:
        raise OauthExchangeError(
            "oauth",
            "unknown or expired state; start the connect flow again",
            context={},
        )
    pending = json.loads(raw)
    provider = pending["provider"]
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": pending["redirect_uri"],
        "code_verifier": pending["code_verifier"],
        **pending["extra_token_params"],
    }
    async with _http() as http:
        try:
            response = await _post_token(
                http,
                pending["token_endpoint"],
                data,
                client_id=pending["client_id"],
                client_secret=pending["client_secret"],
                basic_auth=pending["basic_auth"],
            )
        except httpx.HTTPError as error:
            raise OauthExchangeError(
                provider, f"code exchange failed: {error}", context=pending
            ) from error
    if response.status_code != 200:
        raise OauthExchangeError(
            provider,
            f"code exchange failed: HTTP {response.status_code}",
            context=pending,
        )
    try:
        tokens = response.json()
    except ValueError as error:
        raise OauthExchangeError(
            provider, "the token endpoint returned malformed JSON", context=pending
        ) from error
    if not isinstance(tokens, dict) or not tokens.get("refresh_token"):
        raise OauthExchangeError(
            provider,
            "the authorization server granted no refresh token; druks needs offline access",
            context=pending,
        )
    return tokens, pending


async def fetch_identity(endpoint: str, access_token: str) -> dict:
    """The provider's facts for a fresh token. Any failure returns {} —
    a missing label must not fail the consent."""
    async with _http() as http:
        try:
            response = await http.get(endpoint, headers={"Authorization": f"Bearer {access_token}"})
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return {}
    return payload if isinstance(payload, dict) else {}
