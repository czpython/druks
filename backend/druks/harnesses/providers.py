import base64
import hashlib
import json
import logging
import secrets
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import ClassVar
from urllib.parse import urlencode

import httpx
from pydantic import TypeAdapter

from druks.core.utils.time import ensure_utc
from druks.database import db_session
from druks.redis import get_client
from druks.sandbox.constants import MAX_AGENT_TIMEOUT_SECONDS
from druks.usage.models import UsageScrape

from . import exceptions
from .constants import CONNECT_PENDING_PREFIX, REFRESH_LOCK_PREFIX
from .datastructures import (
    CodexToken,
    CompletedConnect,
    OAuthToken,
    ParsedMetric,
    ParsedUsage,
    ProviderRequest,
    RotationResult,
)
from .models import ProviderCatalog, ProviderLogin

logger = logging.getLogger(__name__)

_GRANT_TIMEOUT_SECONDS = 30.0
_USAGE_TIMEOUT_SECONDS = 20.0
_CATALOG_TIMEOUT_SECONDS = 20.0
_MODELS_DEV_URL = "https://models.dev/api.json"
# The connect-flow pending state (PKCE verifier + state) lives in Redis this
# long — enough to authorize and paste, short enough that an abandoned attempt
# clears.
_CONNECT_PENDING_TTL_SECONDS = 600
# Per-row refresh lock: five minutes outlives the provider grant timeout and
# expires before the next 15-minute cron tick if the holder dies mid-refresh.
_REFRESH_LOCK_TTL_SECONDS = 300

Token = OAuthToken | CodexToken

_WEEKLY_WINDOWS = TypeAdapter(tuple[ParsedMetric, ...])


class Provider:
    """Who answers and bills a model request. A provider owns how its login
    is granted, refreshed, and metered; every harness that drives it runs on that row."""

    id: ClassVar[str]
    label: ClassVar[str]
    # "oauth" for a subscription login, "api_key" for a pasted key.
    login_kinds: ClassVar[frozenset[str]]
    # OAuth refresh config (set by providers with an "oauth" login kind).
    REFRESH_MARGIN: ClassVar[timedelta]
    _TOKEN_URL: ClassVar[str]
    _CLIENT_ID: ClassVar[str]

    @classmethod
    async def connect_start(cls, *, account_id: str | None = None) -> tuple[str, str]:
        """Mint PKCE state under a single-use flow id; return (authorize URL,
        flow id). A flow started by a resolved operator binds ``account_id``."""
        verifier = _b64url(secrets.token_bytes(64))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        url, state = cls.authorize_url(verifier=verifier, challenge=challenge)
        flow_id = secrets.token_urlsafe(24)
        pending = json.dumps(
            {
                "verifier": verifier,
                "state": state,
                "account_id": account_id,
            }
        )
        await get_client().set(
            f"{CONNECT_PENDING_PREFIX}{flow_id}", pending, ex=_CONNECT_PENDING_TTL_SECONDS
        )
        return url, flow_id

    @classmethod
    async def connect_complete(cls, *, flow_id: str, pasted: str) -> CompletedConnect:
        """Pop the flow's single-use state, parse the paste, exchange the code.
        Raises :class:`ConnectError` on failure; the state is gone either way,
        so a retry re-starts cleanly."""
        pending = await get_client().getdel(f"{CONNECT_PENDING_PREFIX}{flow_id}")  # single-use
        if not pending:
            raise exceptions.ConnectError("This connect attempt expired — start it again.")
        expected = json.loads(pending)

        code, pasted_state = _parse_pasted(pasted)
        if not code:
            raise exceptions.ConnectError("Couldn't find an authorization code in what you pasted.")
        if pasted_state and pasted_state != expected["state"]:
            raise exceptions.ConnectError(
                "That code is from a different connect attempt — start it again."
            )

        payload, provider_email = await cls.exchange(code=code, verifier=expected["verifier"])
        if not provider_email:
            raise exceptions.ConnectError(
                "The provider returned no account email — authorize with an account "
                "that has one and try again."
            )
        _, expires_at = cls._refresh_state(payload)
        return CompletedConnect(
            payload=payload,
            provider_email=provider_email,
            expires_at=expires_at,
            account_id=expected["account_id"],
        )

    @classmethod
    def authorize_url(cls, *, verifier: str, challenge: str) -> tuple[str, str]:
        """Build this provider's PKCE authorize URL; return (url, state), where
        ``state`` is what the provider echoes back so connect_complete can
        verify the round-trip."""
        raise NotImplementedError

    @classmethod
    async def exchange(cls, *, code: str, verifier: str) -> tuple[dict, str | None]:
        """Exchange the authorization code for tokens; return (login
        payload, provider-reported account email)."""
        raise NotImplementedError

    @classmethod
    def load_token(cls, login: ProviderLogin, *, now: datetime | None = None) -> Token:
        """Read + validate ``login``'s access token, or raise
        :class:`OAuthTokenError`. Read-only; never refreshes."""
        token = cls._token_from_credentials(dict(login.payload))
        moment = now or _utc_now()
        if token.expires_at and token.expires_at <= moment:
            raise exceptions.OAuthTokenError(
                "token_expired", f"access token expired at {token.expires_at.isoformat()}"
            )
        return token

    @classmethod
    def _token_from_credentials(cls, data: dict) -> Token:
        """Extract the token object from the stored payload; raise
        ``OAuthTokenError('no_token')`` if absent."""
        raise NotImplementedError

    @classmethod
    async def rotate_token(
        cls,
        login_id: str,
        *,
        now: datetime | None = None,
        margin: timedelta | None = None,
    ) -> RotationResult:
        """Refresh one login's token when it is inside the expiry margin.
        A Redis lock elects one refresher per row; the loser reports ``locked``
        and never presents the refresh token a concurrent grant may have burned."""
        moment = now or _utc_now()
        row = await ProviderLogin.reload(login_id)
        if not row:
            return RotationResult(cls.id, "failed", error="no_credentials", login_id=login_id)
        data = dict(row.payload)
        refresh_token, expires_at = cls._refresh_state(data)
        if not refresh_token:
            return RotationResult(cls.id, "no_refresh_token", login_id=login_id)

        limit = margin if margin is not None else cls.REFRESH_MARGIN
        if expires_at and expires_at - moment > limit:
            return RotationResult(cls.id, "fresh", expires_at=expires_at, login_id=login_id)

        redis = get_client()
        lock_key = f"{REFRESH_LOCK_PREFIX}{login_id}"
        if not await redis.set(lock_key, "1", nx=True, ex=_REFRESH_LOCK_TTL_SECONDS):
            return RotationResult(cls.id, "locked", login_id=login_id)
        try:
            # Re-read after winning the lock: the previous holder may have
            # advanced this lineage (or deleted the row) after our first read.
            row = await ProviderLogin.reload(login_id)
            if not row:
                return RotationResult(cls.id, "failed", error="no_credentials", login_id=login_id)
            data = dict(row.payload)
            refresh_token, expires_at = cls._refresh_state(data)
            if not refresh_token:
                return RotationResult(cls.id, "no_refresh_token", login_id=row.id)
            if expires_at and expires_at - moment > limit:
                return RotationResult(cls.id, "fresh", expires_at=expires_at, login_id=row.id)

            try:
                grant = await _post_grant(cls._TOKEN_URL, cls._grant_body(refresh_token))
                new_expiry = cls._apply_refresh(data, grant, moment)
            except exceptions.GrantError as exc:
                if exc.tag == "invalid_grant":
                    # The provider revoked this row's refresh lineage;
                    # presenting it again can never succeed. Drop only this
                    # login so the provider reads as disconnected — the
                    # UI shows Reconnect and the next tick has no row to hammer.
                    await row.delete()
                    await db_session().commit()
                    logger.warning(
                        "%s login %s auto-disconnected after invalid_grant; reconnect to restore",
                        cls.id,
                        row.id,
                    )
                return RotationResult(cls.id, "failed", error=exc.tag, login_id=row.id)
            except ValueError:
                return RotationResult(cls.id, "failed", error="bad_response", login_id=row.id)

            await row.update_payload(data, expires_at=new_expiry)
            # The grant is externally anchored — the provider may have killed
            # the old refresh token the moment it issued this one — so the new
            # lineage must be committed before the lock releases; deferring to
            # the step's own commit would let a concurrent refresher take the
            # freed lock and re-present the superseded token.
            await db_session().commit()
            return RotationResult(cls.id, "refreshed", expires_at=new_expiry, login_id=row.id)
        finally:
            await redis.delete(lock_key)

    @classmethod
    def refresh_is_urgent(cls, login: ProviderLogin) -> bool:
        """Expiry inside the call horizon: a mid-run 401 is unavoidable."""
        _, expires_at = cls._refresh_state(dict(login.payload))
        horizon = timedelta(seconds=MAX_AGENT_TIMEOUT_SECONDS)
        return bool(expires_at) and expires_at - _utc_now() < horizon

    @classmethod
    def needs_refresh(cls, login: ProviderLogin) -> bool:
        """Whether the access token is inside its refresh margin. Unreadable
        or expired reads False: nothing live to protect, rotate ungated."""
        try:
            token = cls._token_from_credentials(dict(login.payload))
        except exceptions.OAuthTokenError:
            return False
        now = _utc_now()
        return bool(token.expires_at) and now < token.expires_at <= now + cls.REFRESH_MARGIN

    @classmethod
    def _refresh_state(cls, data: dict) -> tuple[str | None, datetime | None]:
        """Return (refresh_token, current_expiry) from the stored payload."""
        raise NotImplementedError

    @classmethod
    def _grant_body(cls, refresh_token: str) -> dict:
        """The JSON body for this provider's refresh grant."""
        raise NotImplementedError

    @classmethod
    def _apply_refresh(cls, data: dict, grant: dict, now: datetime) -> datetime | None:
        """Merge the grant response into ``data`` in place; return the new
        expiry. Raise ``ValueError`` if the response is unusable."""
        raise NotImplementedError

    @classmethod
    async def fetch_usage(cls, login: ProviderLogin, *, now: datetime | None = None) -> ParsedUsage:
        """Fetch + parse the login's remaining-quota snapshot from its
        subscription endpoint. Auth/HTTP failures collapse to a
        ``ParsedUsage(ok=False, error=<tag>)`` so they never look like
        '0 metrics'."""
        try:
            token = cls.load_token(login, now=now)
            request = cls._usage_request(token)
        except exceptions.OAuthTokenError as exc:
            return ParsedUsage(ok=False, error=exc.tag)
        except NotImplementedError:
            return ParsedUsage(ok=False, error="unsupported")
        try:
            async with httpx.AsyncClient(timeout=_USAGE_TIMEOUT_SECONDS) as client:
                response = await client.get(request.url, headers=request.headers)
        except httpx.TimeoutException:
            return ParsedUsage(ok=False, error="timeout")
        except httpx.HTTPError as exc:
            logger.warning("usage request failed for %s: %s", cls.id, exc, exc_info=True)
            return ParsedUsage(ok=False, error="network")

        if response.status_code == 200:
            return cls._parse_usage(response.text)
        tag = error_tag(response.status_code)
        logger.warning(
            "usage endpoint %s for %s: %s",
            response.status_code,
            cls.id,
            response.text[:300],
        )
        return ParsedUsage(ok=False, error=tag)

    @classmethod
    async def poll_usage(cls, login: ProviderLogin) -> dict[str, object]:
        """Fetch the login's quota snapshot and persist it as that
        account's UsageScrape row."""
        account_id = login.account_id
        try:
            parsed = await cls.fetch_usage(login)
        except Exception:  # noqa: BLE001 — a crashed scrape records an error row, not a failed refresh
            logger.warning("usage fetch crashed for %s", cls.id, exc_info=True)
            await UsageScrape(
                provider=cls.id,
                account_id=account_id,
                parse_ok=False,
                raw_output=None,
                error="crashed",
            ).save()
            return {
                "provider": cls.id,
                "account_id": account_id,
                "status": "errored",
                "parse_ok": False,
                "error": "crashed",
            }

        snapshot = UsageScrape(
            provider=cls.id,
            account_id=account_id,
            parse_ok=parsed.ok,
            raw_output=parsed.raw[-8000:] if parsed.raw else None,  # cap to avoid bloat
            error=parsed.error if not parsed.ok else None,
            plan_tier=parsed.plan_tier,
            unlimited=parsed.unlimited,
        )
        if parsed.five_hour:
            snapshot.five_hour_percent_left = parsed.five_hour.percent_left
            snapshot.five_hour_resets_at = parsed.five_hour.resets_at
        snapshot.weeks = _WEEKLY_WINDOWS.dump_python(parsed.weeks, mode="json")
        await snapshot.save()
        return {
            "provider": cls.id,
            "account_id": account_id,
            "status": "recorded",
            "parse_ok": parsed.ok,
            "error": parsed.error if not parsed.ok else None,
        }

    @classmethod
    def _usage_request(cls, token: Token) -> ProviderRequest:
        """The authenticated request for the usage endpoint."""
        raise NotImplementedError

    @classmethod
    def _parse_usage(cls, raw: str) -> ParsedUsage:
        """Map the usage endpoint's JSON body into :class:`ParsedUsage`."""
        return ParsedUsage(ok=False, error="unsupported")

    @classmethod
    async def fetch_catalog(cls, login: ProviderLogin) -> tuple[dict, ...]:
        """The models this provider offers, ``{"id", "label"}`` each with ids
        namespaced ``provider/model``. Raises :class:`CatalogError`."""
        request = cls._catalog_request(login)
        try:
            async with httpx.AsyncClient(timeout=_CATALOG_TIMEOUT_SECONDS) as client:
                response = await client.get(request.url, headers=request.headers)
        except httpx.TimeoutException as exc:
            raise exceptions.CatalogError("timeout") from exc
        except httpx.HTTPError as exc:
            raise exceptions.CatalogError("network") from exc
        if response.status_code != 200:
            raise exceptions.CatalogError(error_tag(response.status_code))
        return cls._parse_catalog(response.text)

    @classmethod
    async def refresh_catalog(cls, login: ProviderLogin) -> None:
        """Store a fresh catalog. A failed fetch logs and keeps the stored one."""
        try:
            models = await cls.fetch_catalog(login)
        except (exceptions.CatalogError, exceptions.OAuthTokenError) as exc:
            logger.warning("catalog refresh for %s failed: %s", cls.id, exc.tag)
        else:
            await ProviderCatalog.store(cls.id, list(models))

    @classmethod
    def _catalog_request(cls, login: ProviderLogin) -> ProviderRequest:
        """The request for this provider's model list, authenticated by ``login``."""
        raise NotImplementedError

    @classmethod
    def _parse_catalog(cls, raw: str) -> tuple[dict, ...]:
        """Namespaced ``{"id", "label"}`` entries from the model-list body;
        raises :class:`CatalogError` on a body offering nothing."""
        raise NotImplementedError


def get_providers() -> tuple[Provider, ...]:
    """The registry: one of every ``Provider`` subclass, sorted by id for a
    stable order."""
    return tuple(sorted((provider() for provider in Provider.__subclasses__()), key=lambda p: p.id))


def get_provider(provider_id: str) -> Provider:
    """The provider registered under ``provider_id``; a miss raises ``KeyError``."""
    for provider in get_providers():
        if provider.id == provider_id:
            return provider
    raise KeyError(provider_id)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def jwt_claims(token: str) -> dict | None:
    """Best-effort read of a JWT's claims (no signature check)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, json.JSONDecodeError):
        return
    return claims if isinstance(claims, dict) else None


def jwt_expiry(token: str) -> datetime | None:
    """Best-effort read of a JWT's ``exp`` claim (no signature check)."""
    claims = jwt_claims(token) or {}
    try:
        return datetime.fromtimestamp(claims["exp"], tz=UTC)
    except (KeyError, TypeError, OverflowError, OSError, ValueError):
        return


def parse_epoch_expiry(value: object) -> datetime | None:
    """Claude stores ``expiresAt`` as epoch millis; tolerate seconds."""
    if not isinstance(value, (int, float)):
        return
    seconds = value / 1000 if value > 1e12 else value
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return


def error_tag(status_code: int) -> str:
    return {
        401: "unauthorized",
        403: "forbidden_scope",
        429: "rate_limited",
    }.get(status_code, f"http_{status_code}")


async def _post_grant(url: str, body: dict) -> dict:
    """POST a refresh grant and return the parsed grant dict. Raises
    :class:`GrantError` tagged with why no usable grant came back."""
    try:
        async with httpx.AsyncClient(timeout=_GRANT_TIMEOUT_SECONDS) as client:
            response = await client.post(url, json=body)
    except httpx.HTTPError as exc:
        logger.warning("token refresh request failed (%s): %s", url, exc, exc_info=True)
        raise exceptions.GrantError("network") from exc
    if response.status_code != 200:
        logger.warning(
            "token refresh returned %s (%s): %s",
            response.status_code,
            url,
            response.text[:300],
        )
        if "invalid_grant" in response.text:
            tag = "invalid_grant"
        else:
            tag = f"http_{response.status_code}"
        raise exceptions.GrantError(tag)
    try:
        return response.json()
    except ValueError as exc:
        raise exceptions.GrantError("bad_response") from exc


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _parse_pasted(raw: str) -> tuple[str | None, str | None]:
    """Pull (code, state) out of whatever the operator pasted — a bare code, a
    ``code#state`` pair, a raw query string, or a full redirect URL."""
    value = raw.strip().strip("'\"")
    if not value:
        return None, None
    if "://" in value:
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(value).query))
        return query.get("code"), query.get("state")
    if "#" in value:
        code, _, state = value.partition("#")
        return code, state
    if "code=" in value:
        query = dict(urllib.parse.parse_qsl(value))
        return query.get("code"), query.get("state")
    return value, None


async def post_token(url: str, body: dict, *, form: bool) -> dict:
    """POST an authorization-code exchange (form- or JSON-encoded) and return the
    parsed grant. Raises :class:`ConnectError` with the provider's error text on
    any failure, so the operator sees why the connect didn't take."""
    try:
        async with httpx.AsyncClient(timeout=_GRANT_TIMEOUT_SECONDS) as client:
            if form:
                response = await client.post(url, data=body)
            else:
                response = await client.post(url, json=body)
    except httpx.HTTPError as exc:
        logger.warning("token exchange request failed (%s): %s", url, exc, exc_info=True)
        raise exceptions.ConnectError("The request to the provider failed — try again.") from exc
    if response.status_code != 200:
        logger.warning(
            "token exchange returned %s (%s): %s",
            response.status_code,
            url,
            response.text[:300],
        )
        detail = response.text.strip()[:300] or f"HTTP {response.status_code}"
        raise exceptions.ConnectError(f"The provider rejected the connect: {detail}")
    try:
        return response.json()
    except ValueError as exc:
        raise exceptions.ConnectError("The provider returned an unreadable response.") from exc


_ANTHROPIC_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# Beta flag the Claude CLI sends for OAuth-scoped endpoints.
_OAUTH_BETA = "oauth-2025-04-20"
_ANTHROPIC_VERSION = "2023-06-01"
_CLAUDE_CODE_USER_AGENT = "claude-code/2.1.0"


class AnthropicProvider(Provider):
    id = "anthropic"
    label = "Anthropic"
    login_kinds = frozenset({"oauth", "api_key"})

    REFRESH_MARGIN = timedelta(hours=2)
    _TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
    # Public Claude-Code OAuth client id.
    _CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
    # Connect-flow (PKCE code-paste): authorize on claude.ai, land on the
    # console.anthropic.com code page, exchange JSON with the state echoed in the
    # body.
    redirect_uri = "https://console.anthropic.com/oauth/code/callback"

    @classmethod
    def _token_from_credentials(cls, data: dict) -> OAuthToken:
        block = _oauth_block(data)
        if access := block.get("accessToken") or block.get("access_token"):
            return OAuthToken(
                access_token=access,
                expires_at=parse_epoch_expiry(block.get("expiresAt")),
                scopes=tuple(block.get("scopes") or ()),
                subscription_type=block.get("subscriptionType"),
            )
        raise exceptions.OAuthTokenError("no_token", "credentials file has no access token")

    @classmethod
    def _refresh_state(cls, data: dict) -> tuple[str | None, datetime | None]:
        block = _oauth_block(data)
        refresh = block.get("refreshToken") or block.get("refresh_token")
        return refresh, parse_epoch_expiry(block.get("expiresAt"))

    @classmethod
    def _grant_body(cls, refresh_token: str) -> dict:
        return {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": cls._CLIENT_ID,
        }

    @classmethod
    def authorize_url(cls, *, verifier: str, challenge: str) -> tuple[str, str]:
        # Anthropic's console flow echoes the PKCE verifier back as the OAuth
        # state, so that's what connect_complete checks.
        params = {
            "code": "true",
            "client_id": cls._CLIENT_ID,
            "response_type": "code",
            "redirect_uri": cls.redirect_uri,
            "scope": (
                "org:create_api_key user:profile user:inference "
                "user:sessions:claude_code user:mcp_servers user:file_upload"
            ),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": verifier,
        }
        return f"https://claude.ai/oauth/authorize?{urlencode(params)}", verifier

    @classmethod
    async def exchange(cls, *, code: str, verifier: str) -> tuple[dict, str | None]:
        grant = await post_token(
            cls._TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "client_id": cls._CLIENT_ID,
                "code": code,
                "state": verifier,
                "redirect_uri": cls.redirect_uri,
                "code_verifier": verifier,
            },
            form=False,
        )
        block: dict[str, object] = {
            "accessToken": grant["access_token"],
            "refreshToken": grant["refresh_token"],
            "scopes": (grant.get("scope") or "").split(),
        }
        expires_in = grant.get("expires_in")
        if expires_in:
            expiry = datetime.now(UTC) + timedelta(seconds=expires_in)
            block["expiresAt"] = int(expiry.timestamp() * 1000)
        account = (grant.get("account") or {}).get("email_address")
        return {"claudeAiOauth": block}, account

    @classmethod
    def _apply_refresh(cls, data: dict, grant: dict, now: datetime) -> datetime | None:
        block = _oauth_block(data)
        if access := grant.get("access_token"):
            block["accessToken"] = access
            if grant.get("refresh_token"):
                block["refreshToken"] = grant["refresh_token"]
            expires_in = grant.get("expires_in")
            if isinstance(expires_in, (int, float)):
                new_expiry = now + timedelta(seconds=expires_in)
                block["expiresAt"] = int(new_expiry.timestamp() * 1000)
                return new_expiry
            return parse_epoch_expiry(block.get("expiresAt"))
        raise ValueError("refresh response had no access_token")

    @classmethod
    def _usage_request(cls, token: OAuthToken) -> ProviderRequest:
        return ProviderRequest(_ANTHROPIC_USAGE_URL, cls.oauth_headers(token))

    @classmethod
    def oauth_headers(cls, token: OAuthToken) -> dict:
        return {
            "Authorization": f"Bearer {token.access_token}",
            "anthropic-beta": _OAUTH_BETA,
            "anthropic-version": _ANTHROPIC_VERSION,
            "User-Agent": _CLAUDE_CODE_USER_AGENT,
        }

    @classmethod
    def _parse_usage(cls, raw: str) -> ParsedUsage:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return ParsedUsage(ok=False, error="unparseable", raw=raw)
        if not isinstance(data, dict) or not any(k in data for k in ("five_hour", "seven_day")):
            return ParsedUsage(ok=False, error="unexpected_payload", raw=raw)
        try:
            five_hour, weeks = _claude_windows(data)
        except (KeyError, TypeError, ValueError):
            return ParsedUsage(ok=False, error="unexpected_payload", raw=raw)
        return ParsedUsage(ok=True, five_hour=five_hour, weeks=weeks, raw=raw)

    @classmethod
    def _catalog_request(cls, login: ProviderLogin) -> ProviderRequest:
        if login.kind == "oauth":
            headers = cls.oauth_headers(cls.load_token(login))
        else:
            headers = {
                "x-api-key": login.payload["api_key"],
                "anthropic-version": _ANTHROPIC_VERSION,
            }
        return ProviderRequest("https://api.anthropic.com/v1/models?limit=100", headers)

    @classmethod
    def _parse_catalog(cls, raw: str) -> tuple[dict, ...]:
        try:
            models = tuple(
                {"id": f"{cls.id}/{model['id']}", "label": model.get("display_name") or model["id"]}
                for model in json.loads(raw)["data"]
            )
        except json.JSONDecodeError as exc:
            raise exceptions.CatalogError("unparseable") from exc
        except (KeyError, TypeError, AttributeError) as exc:
            raise exceptions.CatalogError("unexpected_payload") from exc
        if models:
            return models
        raise exceptions.CatalogError("empty_list")


def _oauth_block(data: dict) -> dict:
    """Claude nests the OAuth fields under ``claudeAiOauth``; tolerate a
    flat shape too."""
    block = data.get("claudeAiOauth") if isinstance(data, dict) else None
    return block if isinstance(block, dict) else data


def _claude_windows(data: dict) -> tuple[ParsedMetric | None, tuple[ParsedMetric, ...]]:
    """The binding five-hour window and every weekly window in provider order."""
    five_hour, weekly = [], []
    for limit in data.get("limits") or []:
        # A limit can be scoped to something other than a model, which leaves
        # it counting toward the quota but with no model to name.
        scope = limit["scope"] or {}
        window = ParsedMetric(
            percent_left=max(0, min(100, round(100 - limit["percent"]))),
            resets_at=_parse_iso(limit.get("resets_at")),
            model=(scope.get("model") or {}).get("display_name"),
        )
        if limit["group"] == "weekly":
            weekly.append(window)
        elif limit["group"] == "session":
            five_hour.append(window)
    if not weekly and (fallback_week := _claude_metric(data.get("seven_day"))):
        weekly.append(fallback_week)
    binding_five_hour = ParsedMetric.binding(five_hour) or _claude_metric(data.get("five_hour"))
    weekly_windows = tuple(weekly)
    return binding_five_hour, weekly_windows


def _claude_metric(block: object) -> ParsedMetric | None:
    if not isinstance(block, dict):
        return
    utilization = block.get("utilization")
    percent_left = None
    if isinstance(utilization, (int, float)):
        percent_left = max(0, min(100, int(round(100 - utilization))))
    resets_at = _parse_iso(block.get("resets_at"))
    if percent_left is None and resets_at is None:
        return
    return ParsedMetric(percent_left=percent_left, resets_at=resets_at)


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return
    return ensure_utc(parsed)


# Namespaced claims OpenAI packs into the Codex access-token JWT.
_OPENAI_AUTH_CLAIM = "https://api.openai.com/auth"
_OPENAI_PROFILE_CLAIM = "https://api.openai.com/profile"

# ChatGPT subscription usage endpoint — the standalone fetch the codex CLI's
# account/rateLimits/read RPC uses for the ``chatgpt`` auth app. Returns the
# same numbers /status shows without a completion.
_CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
_CODEX_USER_AGENT = "codex-cli"

# A quota window is named by its declared length, not the slot it arrives in:
# a plan whose only quota is weekly reports it as the primary window.
_WEEKLY_WINDOW_MINIMUM_SECONDS = 24 * 3600


class OpenAiCodexProvider(Provider):
    """The ChatGPT subscription backend (chatgpt.com/backend-api) — pi's
    ``openai-codex`` provider id, and what the codex CLI runs on."""

    id = "openai-codex"
    label = "ChatGPT"
    login_kinds = frozenset({"oauth"})

    REFRESH_MARGIN = timedelta(hours=24)
    _TOKEN_URL = "https://auth.openai.com/oauth/token"
    _CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
    # Connect-flow (PKCE): authorize on auth.openai.com; the operator pastes the
    # failed localhost redirect URL back.
    redirect_uri = "http://localhost:1455/auth/callback"

    @classmethod
    def _token_from_credentials(cls, data: dict) -> CodexToken:
        tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
        if access := tokens.get("access_token"):
            return CodexToken(
                access_token=access,
                expires_at=jwt_expiry(access),
                account_id=tokens.get("account_id"),
            )
        raise exceptions.OAuthTokenError("no_token", "codex auth file has no access token")

    @classmethod
    def _refresh_state(cls, data: dict) -> tuple[str | None, datetime | None]:
        tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
        return tokens.get("refresh_token"), jwt_expiry(tokens.get("access_token") or "")

    @classmethod
    def _grant_body(cls, refresh_token: str) -> dict:
        return {
            "client_id": cls._CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }

    @classmethod
    def authorize_url(cls, *, verifier: str, challenge: str) -> tuple[str, str]:
        state = secrets.token_hex(16)
        params = {
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "originator": "pi",  # the only value verified against the live exchange
            "client_id": cls._CLIENT_ID,
            "response_type": "code",
            "redirect_uri": cls.redirect_uri,
            "scope": "openid profile email offline_access",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        return f"https://auth.openai.com/oauth/authorize?{urlencode(params)}", state

    @classmethod
    async def exchange(cls, *, code: str, verifier: str) -> tuple[dict, str | None]:
        grant = await post_token(
            cls._TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "client_id": cls._CLIENT_ID,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": cls.redirect_uri,
            },
            form=True,
        )
        access = grant["access_token"]
        claims = jwt_claims(access) or {}
        auth = claims.get(_OPENAI_AUTH_CLAIM) or {}
        profile = claims.get(_OPENAI_PROFILE_CLAIM) or {}
        payload = {
            "OPENAI_API_KEY": None,
            "tokens": {
                "access_token": access,
                "refresh_token": grant["refresh_token"],
                "id_token": grant.get("id_token"),
                "account_id": auth.get("chatgpt_account_id"),
            },
            "last_refresh": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        return payload, profile.get("email")

    @classmethod
    def _apply_refresh(cls, data: dict, grant: dict, now: datetime) -> datetime | None:
        if access := grant.get("access_token"):
            tokens = data["tokens"]
            tokens["access_token"] = access
            if grant.get("refresh_token"):
                tokens["refresh_token"] = grant["refresh_token"]
            if grant.get("id_token"):
                tokens["id_token"] = grant["id_token"]
            data["last_refresh"] = now.astimezone(UTC).isoformat().replace("+00:00", "Z")
            return jwt_expiry(access)
        raise ValueError("refresh response had no access_token")

    @classmethod
    def _usage_request(cls, token: CodexToken) -> ProviderRequest:
        return ProviderRequest(_CODEX_USAGE_URL, cls.chatgpt_headers(token))

    @classmethod
    def chatgpt_headers(cls, token: CodexToken) -> dict:
        headers = {
            "Authorization": f"Bearer {token.access_token}",
            "User-Agent": _CODEX_USER_AGENT,
        }
        if token.account_id:
            headers["ChatGPT-Account-Id"] = token.account_id
        return headers

    @classmethod
    def _parse_usage(cls, raw: str) -> ParsedUsage:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return ParsedUsage(ok=False, error="unparseable", raw=raw)
        if not isinstance(data, dict) or "rate_limit" not in data:
            return ParsedUsage(ok=False, error="unexpected_payload", raw=raw)
        plan = data.get("plan_type") if isinstance(data.get("plan_type"), str) else None
        try:
            five_hour, weeks = _codex_windows(data)
            spend = (data.get("spend_control") or {}).get("individual_limit")
            if not five_hour and not weeks and spend:
                # Group-based spend controls replace the rate-limit windows;
                # their weeks-long quota cycle makes it a weekly window.
                weeks = (
                    ParsedMetric(
                        percent_left=max(0, min(100, round(100 - spend["used_percent"]))),
                        resets_at=datetime.fromtimestamp(spend["reset_at"], tz=UTC),
                    ),
                )
        except (AttributeError, KeyError, TypeError, ValueError):
            return ParsedUsage(ok=False, error="unexpected_payload", plan_tier=plan, raw=raw)
        if not five_hour and not weeks:
            # Business/enterprise accounts with unlimited credits carry
            # ``rate_limit: null`` — no windows is the expected shape, not
            # a parse failure. Report permanently-full buckets.
            credits = data.get("credits")
            if isinstance(credits, dict) and credits.get("unlimited"):
                full = ParsedMetric(percent_left=100, resets_at=None)
                return ParsedUsage(
                    ok=True,
                    plan_tier=plan,
                    five_hour=full,
                    weeks=(full,),
                    unlimited=True,
                    raw=raw,
                )
            return ParsedUsage(ok=False, error="parse_failed", plan_tier=plan, raw=raw)
        return ParsedUsage(
            ok=True,
            plan_tier=plan,
            five_hour=five_hour,
            weeks=weeks,
            raw=raw,
        )

    @classmethod
    def _catalog_request(cls, login: ProviderLogin) -> ProviderRequest:
        # ``client_version`` is required and lower-bounds the list (the server
        # returns models with ``minimal_client_version <= client_version``); the
        # high constant asks for the full catalog, and the empty-list guard
        # catches it if the server ever starts rejecting unknown versions.
        return ProviderRequest(
            "https://chatgpt.com/backend-api/codex/models?client_version=99.99.99",
            cls.chatgpt_headers(cls.load_token(login)),
        )

    @classmethod
    def _parse_catalog(cls, raw: str) -> tuple[dict, ...]:
        try:
            models = tuple(
                {
                    "id": f"{cls.id}/{model['slug']}",
                    "label": model.get("display_name") or model["slug"],
                    "efforts": [
                        level["effort"] for level in model.get("supported_reasoning_levels") or []
                    ],
                    "minimal_client_version": model.get("minimal_client_version"),
                }
                for model in json.loads(raw)["models"]
                if model.get("visibility") == "list"
            )
        except json.JSONDecodeError as exc:
            raise exceptions.CatalogError("unparseable") from exc
        except (KeyError, TypeError, AttributeError) as exc:
            raise exceptions.CatalogError("unexpected_payload") from exc
        if models:
            return models
        # A 200 with nothing selectable is what a stale-low client_version
        # produces — never let it read as "no models".
        raise exceptions.CatalogError("empty_list")


def _codex_windows(usage: dict) -> tuple[ParsedMetric | None, tuple[ParsedMetric, ...]]:
    """The binding five-hour window and every weekly window in provider order.

    A window's declared length names it, not the slot it arrives in.
    """
    rate_limits = [(None, usage["rate_limit"] or {})]
    for metered in usage.get("additional_rate_limits") or []:
        rate_limits.append((metered["limit_name"], metered["rate_limit"] or {}))

    five_hour, weekly = [], []
    for model, rate_limit in rate_limits:
        for slot in ("primary_window", "secondary_window"):
            block = rate_limit.get(slot)
            if not block:
                continue
            window = ParsedMetric(
                percent_left=max(0, min(100, round(100 - block["used_percent"]))),
                resets_at=datetime.fromtimestamp(block["reset_at"], tz=UTC),
                model=model,
            )
            if block["limit_window_seconds"] >= _WEEKLY_WINDOW_MINIMUM_SECONDS:
                weekly.append(window)
            else:
                five_hour.append(window)
    return ParsedMetric.binding(five_hour), tuple(weekly)


class OpenAiProvider(Provider):
    """The OpenAI platform API (api.openai.com), pay per token on a key."""

    id = "openai"
    label = "OpenAI"
    login_kinds = frozenset({"api_key"})

    @classmethod
    def _catalog_request(cls, _login: ProviderLogin) -> ProviderRequest:
        return ProviderRequest(_MODELS_DEV_URL, {})

    @classmethod
    def _parse_catalog(cls, raw: str) -> tuple[dict, ...]:
        try:
            models = tuple(
                {"id": f"{cls.id}/{model['id']}", "label": model["name"]}
                for model in json.loads(raw)[cls.id]["models"].values()
            )
        except json.JSONDecodeError as exc:
            raise exceptions.CatalogError("unparseable") from exc
        except (KeyError, TypeError, AttributeError) as exc:
            raise exceptions.CatalogError("unexpected_payload") from exc
        if models:
            return models
        raise exceptions.CatalogError("empty_list")
