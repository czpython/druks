import asyncio
import base64
import hashlib
import json
import secrets
from collections.abc import Callable
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

    ``begin_connect`` returns the consent URL to open; ``complete_connect``
    consumes the callback's single-use state and exchanges the code;
    ``mint_access_token`` serves delivery from the Redis token cache, electing
    one refresher per grant. Grants live on the caller's own rows — mint takes
    the grant object that owns them. A ``Service`` with declared OAuth
    endpoints hands back a configured client via ``get_oauth_client()`` —
    construct directly only when no service holds the client credentials.

    ``provider`` keys every Redis entry — connect state, token cache, refresh
    lock — so all clients constructed with one provider name share them, and
    across a rolling deploy old and new processes elect the same single
    refresher. Completion needs only ``provider``: the begun flow's endpoints
    and client identity ride the stashed state, pinned at begin time so a
    configuration change mid-consent cannot mismatch the PKCE verifier.

    ``basic_auth`` picks HTTP Basic on the token endpoint, for both the code
    exchange and refresh; without it the client credentials travel in the form
    body. ``extra_token_params`` land in both bodies (RFC 8707's ``resource``
    audience binding). Scopes are per authorization, not per client — each
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
        mint_wait_interval_seconds: float = OAUTH_MINT_WAIT_INTERVAL_SECONDS,
        mint_wait_attempts: int = OAUTH_MINT_WAIT_ATTEMPTS,
        http_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self.provider = provider
        self.authorization_endpoint = authorization_endpoint
        self.token_endpoint = token_endpoint
        self.client_id = client_id
        self.client_secret = client_secret
        self.basic_auth = basic_auth
        self.extra_token_params = dict(extra_token_params or {})
        self.mint_wait_interval_seconds = mint_wait_interval_seconds
        self.mint_wait_attempts = mint_wait_attempts
        self._http = http_factory or _http

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
        query. Nothing durable is written here — an abandoned consent simply
        expires."""
        state = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        pending = {
            **(context or {}),
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
            "token_endpoint": self.token_endpoint,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "basic_auth": self.basic_auth,
            "extra_token_params": self.extra_token_params,
        }
        await get_client().set(
            f"{self.provider}:connect:{state}",
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
        query.update(extra_authorize_params or {})
        return f"{self.authorization_endpoint}?{urlencode(query)}"

    async def complete_connect(self, *, state: str, code: str) -> tuple[dict, dict]:
        """The callback half: consume the pending state (single-use, GETDEL)
        and exchange the code + verifier for tokens. Returns ``(tokens,
        context)`` — the token response and the begun flow's stash, the
        caller's begin-time context with the flow's ``token_endpoint``,
        ``client_id``, and ``client_secret`` merged in. A response without a
        ``refresh_token`` is rejected: a grant must survive offline. Nothing
        is cached here — the caller's grant becomes real only when its own
        write commits, and the first mint refreshes from it."""
        raw = await get_client().getdel(f"{self.provider}:connect:{state}")
        if not raw:
            raise OauthExchangeError(
                self.provider,
                "unknown or expired state; start the connect flow again",
                context={},
            )
        pending = json.loads(raw)
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": pending["redirect_uri"],
            "code_verifier": pending["code_verifier"],
            **pending["extra_token_params"],
        }
        async with self._http() as http:
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
                    self.provider, f"code exchange failed: {error}", context=pending
                ) from error
        if response.status_code != 200:
            raise OauthExchangeError(
                self.provider,
                f"code exchange failed: HTTP {response.status_code}",
                context=pending,
            )
        try:
            tokens = response.json()
        except ValueError as error:
            raise OauthExchangeError(
                self.provider, "the token endpoint returned malformed JSON", context=pending
            ) from error
        if not isinstance(tokens, dict) or not tokens.get("refresh_token"):
            raise OauthExchangeError(
                self.provider,
                "the authorization server granted no refresh token; druks needs offline access",
                context=pending,
            )
        return tokens, pending

    async def mint_access_token(self, *, key: str, grant) -> str:
        """The delivery-side token for one grant: the cached access token
        while it lives, else one refreshed through the grant's refresh token.
        The provider may rotate the refresh token on use — two concurrent
        refreshes trip its reuse detection and can revoke the whole grant —
        so Redis elects one refresher per ``key`` (SET NX; the TTL is a crash
        backstop a live refresh cannot outlive). Losers poll for the winner's
        cache fill, for about one token-endpoint round trip, then fail loudly.

        ``grant`` is the caller's own object — typically the row the grant
        lives on — carrying two verbs:

        ``grant.load_refresh_token()`` runs under the refresh lock and must
        observe rotations other processes committed — a naive re-select can
        return a row this transaction already identity-mapped, so read with
        ``populate_existing`` or on a fresh session.

        ``grant.save_refresh_token(rotated)`` receives a rotated refresh
        token and must have committed it before returning: the provider has
        already invalidated the old token, so the write cannot ride an
        enclosing transaction that may later roll back. The cache fills only
        after it returns."""
        redis = get_client()
        token_key = f"{self.provider}:access_token:{key}"
        lock_key = f"{self.provider}:refresh_lock:{key}"
        for _ in range(self.mint_wait_attempts):
            cached = await redis.get(token_key)
            if cached:
                return cast(bytes, cached).decode()
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
                "refresh_token": grant.load_refresh_token(),
                **self.extra_token_params,
            }
            async with self._http() as http:
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
                grant.save_refresh_token(tokens["refresh_token"])
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

    async def evict_access_token(self, key: str) -> None:
        await get_client().delete(f"{self.provider}:access_token:{key}")
