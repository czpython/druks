import base64
import hashlib
import json
import logging
import secrets
import urllib.parse
import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import httpx

from druks.database import db_session
from druks.mcp import models as mcp_models
from druks.mcp.helpers import get_bearer_token_env_var
from druks.redis import get_client
from druks.skills.models import Skill
from druks.usage.models import UsageScrape

from .datastructures import (
    AgentInvocation,
    CodexToken,
    HarnessRunResult,
    OAuthToken,
    ParsedUsage,
    RotationResult,
    SandboxSettings,
)
from .exceptions import (
    GrantError,
    HarnessError,
    HarnessNotConnectedError,
    LoginError,
    OAuthTokenError,
)
from .models import HarnessLogin

if TYPE_CHECKING:
    from druks.sandbox.datastructures import McpServer

logger = logging.getLogger(__name__)

_GRANT_TIMEOUT_SECONDS = 30.0
_USAGE_TIMEOUT_SECONDS = 20.0
# The connect-flow pending state (PKCE verifier + state) lives in Redis this long
# — enough to sign in and paste, short enough that an abandoned attempt clears.
_LOGIN_PENDING_TTL_SECONDS = 600
# Per-row refresh lock: five minutes outlives the provider grant timeout and
# expires before the next 15-minute cron tick if the holder dies mid-refresh.
_REFRESH_LOCK_TTL_SECONDS = 300

# The capability manifest is a plain JSON dict written per AgentCall. Bump when
# the recorded shape changes so a reader can tell manifests apart across
# versions; the value is part of the hash, so a bump reshuffles the buckets.
MANIFEST_SCHEMA_VERSION = 1

Token = OAuthToken | CodexToken


class Harness(ABC):
    name: str
    # Harness identity + shipped config, the seed source for the per-harness
    # ``HarnessSettings`` row the operator then tunes. Subclasses set provider,
    # model_prefixes, models and default_model; effort/timeout default to the
    # shipped values.
    provider: ClassVar[str]
    # The model-name namespaces this harness runs — a model routes to the harness
    # that owns its namespace, so a new model in a known one runs with no release.
    model_prefixes: ClassVar[tuple[str, ...]]
    # Suggested models for the settings picker and the ``default_model`` seed —
    # advisory: any string in a matching namespace runs.
    models: ClassVar[tuple[str, ...]]
    default_model: ClassVar[str]
    default_effort: ClassVar[str] = "high"
    default_timeout: ClassVar[int] = 1800
    # Per-CLI OAuth refresh config (set by subclasses).
    REFRESH_MARGIN: timedelta
    _TOKEN_URL: str
    _CLIENT_ID: str

    @classmethod
    def has_model(cls, model: str) -> bool:
        """Whether ``model`` runs on this harness — matched by name namespace, so
        a new model in a known namespace routes with no release."""
        return model == cls.name or model.startswith(cls.model_prefixes)

    def __init__(
        self,
        *,
        model: str | None,
        fast_mode: bool,
        effort: str | None,
        sandbox: SandboxSettings | None = None,
    ) -> None:
        self.model = model
        self.fast_mode = fast_mode
        self.effort = effort
        # Optional only so argv-shape unit tests can build the harness without a
        # sandbox-configured Settings; every real run needs it and raises when None.
        self.sandbox = sandbox

    @abstractmethod
    def build_invocation(self, **kwargs: object) -> AgentInvocation:
        """Assemble this CLI's full invocation (argv, stdin, credentials,
        env) for one prompt. Pure — never touches the live sandbox; the
        sandbox executes the returned invocation."""

    @abstractmethod
    def parse(self, result: HarnessRunResult, *, artifact_dir: Path, run_id: str) -> object:
        """Turn a finished run into the structured payload (and write the
        cost/output sidecars under ``artifact_dir / run_id``)."""

    def get_manifest(
        self,
        *,
        mcp_servers: tuple["McpServer", ...],
        extra_env: dict[str, str] | None,
    ) -> dict:
        """The capability manifest for one AgentCall: what this harness was
        handed. Presence only — a token records as a boolean, never its value,
        so nothing here needs scrubbing. Identity stays off it — the manifest
        sits in the call dir whose name is the AgentCall id, and that row
        already records which agent ran; the execution record (args, timings,
        exit code) is metadata.json beside it.

        Everything recorded is capability-shaped and hashed: ``manifest_hash``
        is a stable digest of the canonicalised record, so an identical
        capability set always hashes the same and an eval report can bucket
        calls by it."""
        delivered_env = extra_env or {}
        # Declared = the enabled registry view; delivered = what actually
        # reached this call (a workspace's required server owns its name — see
        # Workspace.with_mcp_servers). The delivered server is what
        # this harness ran against, so record its url + env var; fall back to
        # the declared values only for a declared-but-not-delivered entry.
        # token_present reads the delivered env: a server's bearer env var is
        # set iff its token was found at delivery, for a static or an
        # app-minted token alike.
        declared = {server["name"]: server for server in mcp_models.McpServer.list_enabled()}
        delivered_by_name = {server.name: server for server in mcp_servers}
        mcp = []
        for name in sorted(declared.keys() | delivered_by_name.keys()):
            server = delivered_by_name.get(name)
            env_var = server.bearer_token_env_var if server else get_bearer_token_env_var(name)
            mcp.append(
                {
                    "name": name,
                    "url": server.url if server else declared[name]["url"],
                    "bearer_token_env_var": env_var,
                    "declared": name in declared,
                    "delivered": name in delivered_by_name,
                    "token_present": env_var in delivered_env,
                }
            )
        # The skills tar both harnesses push excludes disabled skills, so the
        # enabled set — not merely "a tree exists" — is the call's real skill
        # capability.
        capability = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "model": self.model or "",
            "harness": self.name,
            "mcp_servers": mcp,
            "skills_enabled": sorted(skill.name for skill in Skill.list_enabled()),
        }
        canonical = json.dumps(capability, sort_keys=True, separators=(",", ":"))
        return {
            "manifest_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            **capability,
        }

    @staticmethod
    def mint_run_id(call_id: str | None) -> str:
        """Identifier for the in-VM ``$get_runs_root/<id>/`` directory.

        Must be unique per call: the helper reuses ``$get_runs_root/<id>/``
        across invocations of the same id, leaves stale ``exit_code``
        behind, and the orchestrator's stat-based ``_is_done()`` poll
        reads it before the new helper can overwrite. Prefer the
        orchestrator-supplied id so the in-VM dir is the canonical row
        reference; fall back to a fresh uuid
        for one-shot callers without parent state.
        """
        return call_id or str(uuid.uuid4())

    @classmethod
    def get_credentials(cls) -> dict:
        """The default seat's credential-file dict — the row execution resolves
        while runs aren't seat-aware. Raises :class:`HarnessNotConnectedError`,
        with the connect-in-Settings fix, when no seat is designated."""
        row = HarnessLogin.get_default(cls.name)
        data = dict(row.payload) if row else None
        if not data:
            raise HarnessNotConnectedError(
                f"{cls.name} is not connected — connect it in Settings → Harnesses."
            )
        return data

    @classmethod
    def render_credentials_file(cls) -> str:
        """The credential-file JSON the sandbox writes for this CLI — the stored
        payload serialized. The push writes it as a secret; no host credential
        file is read. Raises :class:`HarnessNotConnectedError` when the harness
        isn't connected."""
        return json.dumps(cls.get_credentials())

    @classmethod
    async def login_start(cls) -> tuple[str, str]:
        """Begin a connect flow: mint a PKCE verifier + challenge, build the
        provider's authorize URL, and stash the pending state in Redis under an
        opaque flow id (single-use, short TTL) so concurrent connects never
        overwrite each other. Returns (authorize URL, flow id)."""
        verifier = _b64url(secrets.token_bytes(64))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        url, state = cls.authorize_url(verifier=verifier, challenge=challenge)
        flow_id = secrets.token_urlsafe(24)
        pending = json.dumps({"harness": cls.name, "verifier": verifier, "state": state})
        await get_client().set(_login_pending_key(flow_id), pending, ex=_LOGIN_PENDING_TTL_SECONDS)
        return url, flow_id

    @classmethod
    async def login_complete(cls, *, flow_id: str, pasted: str) -> None:
        """Finish a connect flow: pop the flow's single-use pending state,
        parse the paste (bare code or full redirect URL), exchange it, and
        upsert the seat under the provider-reported account. Raises
        :class:`LoginError` with a user-facing message on any failure — the
        pending state is gone either way, so a retry re-starts cleanly."""
        pending = await get_client().getdel(_login_pending_key(flow_id))  # single-use
        if not pending:
            raise LoginError("This sign-in expired — start it again.")
        expected = json.loads(pending)
        if expected["harness"] != cls.name:
            raise LoginError("That sign-in belongs to a different harness — start it again.")

        code, pasted_state = _parse_pasted(pasted)
        if not code:
            raise LoginError("Couldn't find an authorization code in what you pasted.")
        if pasted_state and pasted_state != expected["state"]:
            raise LoginError("That code is from a different sign-in — start it again.")

        payload, provider_email = await cls.exchange(code=code, verifier=expected["verifier"])
        if not provider_email:
            raise LoginError(
                "The provider returned no account email — sign in with an account "
                "that has one and try again."
            )
        _, expires_at = cls._refresh_state(payload)
        HarnessLogin.connect(
            harness=cls.name,
            payload=payload,
            expires_at=expires_at,
            provider_email=provider_email,
        )

    @classmethod
    def disconnect(cls) -> None:
        """Disconnect the default seat — the single connection card's target.
        Never promotes another seat."""
        row = HarnessLogin.get_default(cls.name)
        if row:
            row.delete()

    @classmethod
    @abstractmethod
    def authorize_url(cls, *, verifier: str, challenge: str) -> tuple[str, str]:
        """Build this provider's PKCE authorize URL; return (url, state), where
        ``state`` is what the provider echoes back so login_complete can verify
        the round-trip."""

    @classmethod
    @abstractmethod
    async def exchange(cls, *, code: str, verifier: str) -> tuple[dict, str | None]:
        """Exchange the authorization code for tokens; return (credential-file
        payload, provider-reported account email)."""

    @classmethod
    def load_token(cls, *, now: datetime | None = None) -> Token:
        """Read + validate the current access token, or raise
        :class:`OAuthTokenError`. Read-only; never refreshes."""
        try:
            data = cls.get_credentials()
        except HarnessNotConnectedError as exc:
            raise OAuthTokenError("no_credentials", f"no stored {cls.name} credentials") from exc
        token = cls._token_from_credentials(data)
        moment = now or _utc_now()
        if token.expires_at and token.expires_at <= moment:
            raise OAuthTokenError(
                "token_expired", f"access token expired at {token.expires_at.isoformat()}"
            )
        return token

    @classmethod
    @abstractmethod
    def _token_from_credentials(cls, data: dict) -> Token:
        """Extract the token object from the parsed credential file;
        raise ``OAuthTokenError('no_token')`` if absent."""

    @classmethod
    async def rotate_token(
        cls,
        login_id: str,
        *,
        now: datetime | None = None,
        margin: timedelta | None = None,
    ) -> RotationResult:
        """Refresh one login row's token if it's within the expiry margin,
        persisting the new token back to that row. Addressed by row id and
        read fresh — the caller's tick may span other rows' commits. One
        refresher per row: a Redis lock elects the winner, and the loser
        reports ``locked`` without touching the provider — two concurrent
        grants on one refresh lineage trip the provider's reuse detection."""
        moment = now or _utc_now()
        row = HarnessLogin.reload(login_id)
        if not row:
            return RotationResult(cls.name, "failed", error="no_credentials", login_id=login_id)
        data = dict(row.payload)
        refresh_token, expires_at = cls._refresh_state(data)
        if not refresh_token:
            return RotationResult(cls.name, "no_refresh_token", login_id=login_id)

        limit = margin if margin is not None else cls.REFRESH_MARGIN
        if expires_at and expires_at - moment > limit:
            return RotationResult(cls.name, "fresh", expires_at=expires_at, login_id=login_id)

        redis = get_client()
        lock_key = _refresh_lock_key(login_id)
        if not await redis.set(lock_key, "1", nx=True, ex=_REFRESH_LOCK_TTL_SECONDS):
            return RotationResult(cls.name, "locked", login_id=login_id)
        try:
            # Re-read after winning the lock: the previous holder may have
            # advanced this lineage (or deleted the row) after our first read.
            row = HarnessLogin.reload(login_id)
            if not row:
                return RotationResult(
                    cls.name, "failed", error="no_credentials", login_id=login_id
                )
            data = dict(row.payload)
            refresh_token, expires_at = cls._refresh_state(data)
            if not refresh_token:
                return RotationResult(cls.name, "no_refresh_token", login_id=row.id)
            if expires_at and expires_at - moment > limit:
                return RotationResult(cls.name, "fresh", expires_at=expires_at, login_id=row.id)

            try:
                grant = await _post_grant(cls._TOKEN_URL, cls._grant_body(refresh_token))
                new_expiry = cls._apply_refresh(data, grant, moment)
            except GrantError as exc:
                if exc.tag == "invalid_grant":
                    # The provider revoked this row's refresh lineage;
                    # presenting it again can never succeed. Drop only this
                    # credential so the seat reads as disconnected — the UI
                    # shows Reconnect and the next tick has no row to hammer.
                    row.delete()
                    db_session().commit()
                    logger.warning(
                        "%s login %s auto-disconnected after invalid_grant; "
                        "reconnect to restore",
                        cls.name,
                        row.id,
                    )
                return RotationResult(cls.name, "failed", error=exc.tag, login_id=row.id)
            except ValueError:
                return RotationResult(cls.name, "failed", error="bad_response", login_id=row.id)

            row.update_payload(data, expires_at=new_expiry)
            # The grant is externally anchored — the provider may have killed
            # the old refresh token the moment it issued this one — so the new
            # lineage must be committed before the lock releases; deferring to
            # the step's own commit would let a concurrent refresher take the
            # freed lock and re-present the superseded token.
            db_session().commit()
            return RotationResult(cls.name, "refreshed", expires_at=new_expiry, login_id=row.id)
        finally:
            await redis.delete(lock_key)

    @classmethod
    def needs_refresh(cls, login: HarnessLogin) -> bool:
        """Whether the row's access token is within its refresh margin — the
        cheap read the refresh workflow uses to decide whether to gate
        provisioning before rotating. An unreadable/expired credential reads
        as False: there's nothing live to protect, so let the rotation sort it
        out ungated."""
        try:
            token = cls._token_from_credentials(dict(login.payload))
        except OAuthTokenError:
            return False
        if not token.expires_at:
            return False
        now = _utc_now()
        if token.expires_at <= now:
            return False
        return token.expires_at - now <= cls.REFRESH_MARGIN

    @classmethod
    @abstractmethod
    def _refresh_state(cls, data: dict) -> tuple[str | None, datetime | None]:
        """Return (refresh_token, current_expiry) from the credential file."""

    @classmethod
    @abstractmethod
    def _grant_body(cls, refresh_token: str) -> dict:
        """The JSON body for this CLI's refresh grant."""

    @classmethod
    @abstractmethod
    def _apply_refresh(cls, data: dict, grant: dict, now: datetime) -> datetime | None:
        """Merge the grant response into ``data`` in place; return the new
        expiry. Raise ``ValueError`` if the response is unusable."""

    @classmethod
    async def fetch_usage(cls, *, now: datetime | None = None) -> ParsedUsage:
        """Fetch + parse this CLI's remaining-quota snapshot from its
        subscription endpoint. Auth/HTTP failures collapse to a
        ``ParsedUsage(ok=False, error=<tag>)`` so they never look like
        '0 metrics'."""
        try:
            token = cls.load_token(now=now)
        except OAuthTokenError as exc:
            return ParsedUsage(ok=False, error=exc.tag)

        url, headers = cls._usage_request(token)
        try:
            async with httpx.AsyncClient(timeout=_USAGE_TIMEOUT_SECONDS) as client:
                response = await client.get(url, headers=headers)
        except httpx.TimeoutException:
            return ParsedUsage(ok=False, error="timeout")
        except httpx.HTTPError as exc:
            logger.warning("usage request failed for %s: %s", cls.name, exc, exc_info=True)
            return ParsedUsage(ok=False, error="network")

        if response.status_code == 200:
            return cls._parse_usage(response.text)
        tag = _error_tag(response.status_code)
        logger.warning(
            "usage endpoint %s for %s: %s",
            response.status_code,
            cls.name,
            response.text[:300],
        )
        return ParsedUsage(ok=False, error=tag)

    @classmethod
    async def poll_usage(cls) -> dict[str, object]:
        """Fetch this harness's quota snapshot and persist it as a UsageScrape row."""
        try:
            parsed = await cls.fetch_usage()
        except Exception:  # noqa: BLE001 — one broken harness must not lose the others' snapshots
            logger.warning("usage fetch crashed for %s", cls.name, exc_info=True)
            UsageScrape(harness=cls.name, parse_ok=False, raw_output=None, error="crashed").save()
            return {"harness": cls.name, "status": "errored", "parse_ok": False, "error": "crashed"}

        snapshot = UsageScrape(
            harness=cls.name,
            parse_ok=parsed.ok,
            raw_output=parsed.raw[-8000:] if parsed.raw else None,  # cap to avoid bloat
            error=parsed.error if not parsed.ok else None,
            plan_tier=parsed.plan_tier,
            unlimited=parsed.unlimited,
        )
        if parsed.five_hour:
            snapshot.five_hour_percent_left = parsed.five_hour.percent_left
            snapshot.five_hour_resets_at = parsed.five_hour.resets_at
        if parsed.week:
            snapshot.week_percent_left = parsed.week.percent_left
            snapshot.week_resets_at = parsed.week.resets_at
        snapshot.save()
        return {
            "harness": cls.name,
            "status": "recorded",
            "parse_ok": parsed.ok,
            "error": parsed.error if not parsed.ok else None,
        }

    @classmethod
    @abstractmethod
    def _usage_request(cls, token: Token) -> tuple[str, dict]:
        """Return (url, headers) for the usage endpoint."""

    @classmethod
    @abstractmethod
    def _parse_usage(cls, raw: str) -> ParsedUsage:
        """Map the usage endpoint's JSON body into :class:`ParsedUsage`."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def jwt_claims(token: str) -> dict | None:
    """Best-effort read of a JWT's claims (no signature check)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, json.JSONDecodeError):
        return None
    return claims if isinstance(claims, dict) else None


def jwt_expiry(token: str) -> datetime | None:
    """Best-effort read of a JWT's ``exp`` claim (no signature check)."""
    claims = jwt_claims(token) or {}
    try:
        return datetime.fromtimestamp(claims["exp"], tz=UTC)
    except (KeyError, TypeError, OverflowError, OSError, ValueError):
        return None


def parse_epoch_expiry(value: object) -> datetime | None:
    """Claude stores ``expiresAt`` as epoch millis; tolerate seconds."""
    if not isinstance(value, (int, float)):
        return None
    seconds = value / 1000 if value > 1e12 else value
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _error_tag(status_code: int) -> str:
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
        raise GrantError("network") from exc
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
        raise GrantError(tag)
    try:
        return response.json()
    except ValueError as exc:
        raise GrantError("bad_response") from exc


def _login_pending_key(flow_id: str) -> str:
    return f"druks:login:pending:{flow_id}"


def _refresh_lock_key(login_id: str) -> str:
    return f"druks:harness:refresh:{login_id}"


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
    parsed grant. Raises :class:`LoginError` with the provider's error text on any
    failure, so the operator sees why the sign-in didn't take."""
    try:
        async with httpx.AsyncClient(timeout=_GRANT_TIMEOUT_SECONDS) as client:
            if form:
                response = await client.post(url, data=body)
            else:
                response = await client.post(url, json=body)
    except httpx.HTTPError as exc:
        logger.warning("token exchange request failed (%s): %s", url, exc, exc_info=True)
        raise LoginError("The request to the provider failed — try again.") from exc
    if response.status_code != 200:
        logger.warning(
            "token exchange returned %s (%s): %s",
            response.status_code,
            url,
            response.text[:300],
        )
        detail = response.text.strip()[:300] or f"HTTP {response.status_code}"
        raise LoginError(f"The provider rejected the sign-in: {detail}")
    try:
        return response.json()
    except ValueError as exc:
        raise LoginError("The provider returned an unreadable response.") from exc


def check_returncode(result: HarnessRunResult, *, name: str) -> None:
    if result.returncode != 0:
        detail = _terminal_detail(result.stdout)
        raise HarnessError(f"{name} exited with {result.returncode}.{detail}")


def _terminal_detail(stdout: bytes) -> str:
    """The CLI's terminal error ("You've hit your session limit · resets
    5:10pm") rides the stream's last result event; without it the persisted
    failure is a bare exit code and the operator has to dig transcripts."""
    for line in reversed(stdout.splitlines()[-20:]):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("is_error") and isinstance(event.get("result"), str):
            return f" {event['result'][:300]}"
        error = event.get("error")
        if isinstance(error, dict):
            error = error.get("message")
        if isinstance(error, str) and error:
            return f" {error[:300]}"
    return ""
