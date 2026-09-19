import asyncio
import base64
import hashlib
import json
import secrets
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import urlencode

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from druks.database import get_session
from druks.exceptions import LockHeldError
from druks.locks import lock
from druks.redis import get_client
from druks.secrets.models import VaultSecret
from druks.signals import publish

from .constants import (
    OAUTH_CONNECT_STATE_TTL_SECONDS,
    OAUTH_MINT_WAIT_ATTEMPTS,
    OAUTH_MINT_WAIT_INTERVAL_SECONDS,
    OAUTH_TOKEN_TTL_SKEW_SECONDS,
)
from .exceptions import OauthExchangeError, OauthRefreshError


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
    # RFC 6749: HTTP Basic keeps the client credentials out of the form body.
    if basic_auth:
        return await http.post(token_endpoint, data=data, auth=(client_id, client_secret))
    data["client_id"] = client_id
    if client_secret:
        data["client_secret"] = client_secret
    return await http.post(token_endpoint, data=data)


class OauthClient:
    """One provider's OAuth 2.0 authorization-code flow with PKCE, and a refresh
    that is safe when the provider rotates refresh tokens. Use it for a provider
    with fixed endpoints and a pre-registered client::

        client = OauthClient(
            provider="acme",
            authorization_endpoint="https://acme.example/oauth/authorize",
            token_endpoint="https://acme.example/oauth/token",
            client_id=..., client_secret=...,
            basic_auth=True,
        )

    ``begin_connect`` returns the consent URL. ``complete_connect`` consumes the
    callback's single-use state and exchanges the code. The caller stores the
    grant in the vault and passes it to ``get_access_token``. A ``Service`` with
    declared OAuth endpoints returns a configured client from
    ``get_oauth_client()``. Construct a client directly only when no service
    holds the client credentials.

    The token cache and the refresh lock key on the connection id. All clients
    for one provider share them, also across a rolling deploy. The consent stash
    pins the endpoints and the client at begin time, so a configuration change
    during a consent cannot break its exchange.

    ``basic_auth`` sends the client credentials with HTTP Basic, else in the form
    body. ``extra_token_params`` go into the code exchange and the refresh, for
    example RFC 8707's ``resource``. ``extra_authorize_params`` go into every
    consent query, for example Google's ``access_type=offline`` and
    ``prompt=consent``. Each ``begin_connect`` asks for its own scopes.
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
        """Stash the pending exchange in Redis under a new single-use state, and
        return the consent URL. ``context`` comes back from ``complete_connect``.
        ``extra_authorize_params`` override the client's declared ones on the same
        key. Nothing durable is written, so an abandoned consent expires."""
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
        session: AsyncSession,
        *,
        connection: VaultSecret,
        scopes: tuple[str, ...] = (),
        cached: bool = True,
    ) -> tuple[str, datetime | None]:
        """The access token for one grant, and its expiry. A cached token serves
        while it lives. Else Redis elects one refresher per connection and scope
        set, because two concurrent refreshes can trip the provider's reuse
        detection and revoke the grant. Other callers wait for its result, then
        raise. The expiry is the provider's ``expires_in`` less the skew, one hour
        when it gives none, or None for a cached token without a lifetime.

        ``scopes`` asks for a token narrower than the grant (RFC 6749 section 6),
        for a token that goes to untrusted compute. They must be a subset of the
        granted scopes. ``cached=False`` skips the cache read, but still elects
        one refresher and fills the cache."""
        if connection.revoked_at:
            raise OauthRefreshError(
                self.provider, "the connection is revoked; sign in again to restore it"
            )
        requested = tuple(sorted(scopes))
        granted = set(connection.scopes or [])
        if requested and not set(requested) <= granted:
            missing = ", ".join(sorted(set(requested) - granted))
            raise OauthRefreshError(
                self.provider, f"the connection does not grant scope(s) {missing}"
            )
        redis = get_client()
        # A down-scoped token must never serve a full-scope caller, or the reverse.
        suffix = ""
        if requested:
            suffix = ":" + hashlib.sha256(" ".join(requested).encode()).hexdigest()[:16]
        token_key = f"{self.provider}:access_token:{connection.id}{suffix}"
        lock_key = f"{self.provider}:refresh_lock:{connection.id}{suffix}"
        refresh_lock = AsyncExitStack()
        for _ in range(self.mint_wait_attempts):
            if cached:
                cached_token = await redis.get(token_key)
                if cached_token:
                    ttl = await redis.ttl(token_key)
                    return cast(bytes, cached_token).decode(), _expiry(ttl)
            try:
                await refresh_lock.enter_async_context(lock(lock_key, blocking=False))
            except LockHeldError:
                await asyncio.sleep(self.mint_wait_interval_seconds)
            else:
                break
        else:
            raise OauthRefreshError(
                self.provider, "timed out waiting for a concurrent refresh to finish"
            )
        async with refresh_lock:
            data = {
                "grant_type": "refresh_token",
                "refresh_token": await connection.get_refresh_token(),
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
                if "invalid_grant" in response.text:
                    # The provider withdrew the grant; presenting it again can never
                    # succeed. The revoke commits on its own: the caller's step
                    # session rolls back when this error propagates.
                    async with get_session(session.bind) as own:
                        revoked = await own.get(VaultSecret, connection.id)
                        await self.disconnect(revoked, reason="invalid_grant")
                        await own.commit()
                    raise OauthRefreshError(
                        self.provider,
                        "the provider revoked the grant; sign in again to restore the connection",
                    )
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
                await connection.update_refresh_token(tokens["refresh_token"])
            if requested and tokens.get("scope") and set(tokens["scope"].split()) != set(requested):
                # The provider ignored the narrowing. The sandbox must never hold this token.
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
            return tokens["access_token"], _expiry(ttl)

    async def evict_access_token(self, connection_id: str) -> None:
        # Down-scoped tokens share the prefix, so one scan removes them all.
        redis = get_client()
        async for key in redis.scan_iter(match=f"{self.provider}:access_token:{connection_id}*"):
            await redis.delete(key)

    async def disconnect(self, connection: VaultSecret, *, reason: str) -> None:
        """Revoke the grant, evict its cached access token, and publish
        ``oauth.disconnected``."""
        await connection.revoke(reason)
        await self.evict_access_token(connection.id)
        await publish(
            "oauth.disconnected",
            provider=self.provider,
            connection_id=connection.id,
            account_id=connection.account_id,
        )


def _expiry(seconds: int) -> datetime | None:
    # Redis returns -1 for a key without a lifetime and -2 for a missing key.
    return datetime.now(UTC) + timedelta(seconds=seconds) if seconds > 0 else None


async def complete_connect(*, state: str, code: str) -> tuple[dict, dict]:
    """Consume the single-use state and exchange the code. Returns ``(tokens,
    pending)``. The caller stores the grant, because only it knows the account."""
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
    """The provider's facts for a fresh token, or {} on any failure, so a missing
    label never fails the consent."""
    async with _http() as http:
        try:
            response = await http.get(endpoint, headers={"Authorization": f"Bearer {access_token}"})
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return {}
    return payload if isinstance(payload, dict) else {}
