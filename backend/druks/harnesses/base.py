import hashlib
import json
import logging
import uuid
from abc import ABC, abstractmethod
from collections.abc import Collection
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import httpx

from druks.mcp import models as mcp_models
from druks.mcp.helpers import get_bearer_token_env_var
from druks.skills.models import Skill

from . import exceptions
from .datastructures import (
    AgentInvocation,
    HarnessRunResult,
    ParsedModels,
    ProviderRequest,
    SandboxSettings,
)
from .exceptions import OAuthTokenError
from .models import ProviderLogin
from .providers import Token, error_tag, get_provider

if TYPE_CHECKING:
    from druks.sandbox.datastructures import McpServer

logger = logging.getLogger(__name__)

_DISCOVERY_TIMEOUT_SECONDS = 20.0

# The capability manifest is a plain JSON dict written per AgentCall. Bump when
# the recorded shape changes so a reader can tell manifests apart across
# versions; the value is part of the hash, so a bump reshuffles the buckets.
MANIFEST_SCHEMA_VERSION = 2


class Harness(ABC):
    name: str
    command: ClassVar[str]
    # The login kinds this CLI consumes: "oauth" for a subscription CLI,
    # "api_key" for one that runs on a pasted key.
    login_kinds: ClassVar[frozenset[str]]
    # The one provider a subscription CLI is bound to. None for a key CLI,
    # which runs whichever provider the model names.
    provider: ClassVar[str | None] = None
    # Suggested models for the settings picker and the ``default_model`` seed.
    models: ClassVar[tuple[str, ...]]
    default_model: ClassVar[str]
    # This CLI's terminal-error vocabulary: phrase → the failure it names, the
    # first phrase found in a death's text winning in declaration order; no
    # match stays a bare, never-retried HarnessError. The terminal event has no
    # structured error subtype, so words are the only signal.
    failure_markers: ClassVar[dict[str, type[exceptions.HarnessError]]] = {}
    default_effort: ClassVar[str] = "high"
    default_timeout: ClassVar[int] = 1800
    # Claude's slowest measured cold start is under 60 seconds. This margin
    # covers slow MCP loads, token refresh, and prompt assembly.
    first_byte_seconds: ClassVar[int | None] = 90

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
    async def build_invocation(self, **kwargs: object) -> AgentInvocation:
        """Assemble this CLI's full invocation (argv, stdin, credentials,
        env) for one prompt. Pure — never touches the live sandbox; the
        sandbox executes the returned invocation."""

    @abstractmethod
    def parse(self, result: HarnessRunResult, *, artifact_dir: Path, run_id: str) -> object:
        """Turn a finished run into the structured payload (and write the
        cost/output sidecars under ``artifact_dir / run_id``)."""

    @classmethod
    def check_returncode(cls, result: HarnessRunResult) -> None:
        if result.returncode != 0:
            detail = _terminal_detail(result)
            message = f"{cls.name} exited with {result.returncode}.{detail}"
            lowered = detail.lower()
            for marker, error in cls.failure_markers.items():
                if marker in lowered:
                    raise error(message)
            raise exceptions.HarnessError(message)

    @classmethod
    def provider_for(cls, model: str) -> str:
        """The provider whose login ``model`` spends: its namespace, else the
        provider this CLI is bound to."""
        provider, separator, _ = model.partition("/")
        if separator:
            return provider
        if cls.provider:
            return cls.provider
        raise exceptions.HarnessError(f"{cls.name} needs a provider namespace on model {model!r}.")

    @classmethod
    def accepts(cls, login: ProviderLogin) -> bool:
        """Whether this CLI runs on ``login``."""
        bound = not cls.provider or cls.provider == login.provider
        return bound and login.kind in cls.login_kinds

    async def login(self, login_id: str | None) -> ProviderLogin:
        """The selected login, read fresh at push time; with none
        selected, the fallback account's for this model's provider. A vanished
        row fails the call rather than render another account's."""
        if login_id:
            if row := await ProviderLogin.get(login_id):
                return row
            raise exceptions.HarnessNotConnectedError(
                "the selected login was removed — reconnect it in Settings → Providers."
            )
        return await ProviderLogin.lookup(self.provider_for(self.model), None)

    async def get_manifest(
        self,
        *,
        mcp_servers: tuple["McpServer", ...],
        skills: Collection[str],
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
        declared = {server["name"]: server for server in await mcp_models.McpServer.list_enabled()}
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
        # Only the delivered skill set is reachable in either CLI home, so the
        # manifest records that per-call capability.
        capability = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "model": self.model or "",
            "harness": self.name,
            "mcp_servers": mcp,
            "skills_delivered": sorted(skill.name for skill in await Skill.list_delivered(skills)),
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
    async def fetch_models(cls, login: ProviderLogin) -> ParsedModels:
        """Fetch + parse the provider's selectable-model list for the settings
        picker. Auth/HTTP failures collapse to a ``ParsedModels(ok=False,
        error=<tag>)`` so they never look like 'no models' — the stored list
        only ever advances, it is never wiped by a bad fetch."""
        try:
            token = get_provider(login.provider).load_token(login)
            request = cls._model_discovery_request(token)
        except OAuthTokenError as exc:
            return ParsedModels(ok=False, error=exc.tag)
        except NotImplementedError:
            return ParsedModels(ok=False, error="unsupported")
        try:
            async with httpx.AsyncClient(timeout=_DISCOVERY_TIMEOUT_SECONDS) as client:
                response = await client.get(request.url, headers=request.headers)
        except httpx.TimeoutException:
            return ParsedModels(ok=False, error="timeout")
        except httpx.HTTPError as exc:
            logger.warning("models request failed for %s: %s", cls.name, exc, exc_info=True)
            return ParsedModels(ok=False, error="network")

        if response.status_code == 200:
            return cls._parse_models(response.text)
        tag = error_tag(response.status_code)
        logger.warning(
            "models endpoint %s for %s: %s",
            response.status_code,
            cls.name,
            response.text[:300],
        )
        return ParsedModels(ok=False, error=tag)

    @classmethod
    def _model_discovery_request(cls, token: Token) -> ProviderRequest:
        """The authenticated request for the model-discovery endpoint."""
        raise NotImplementedError

    @classmethod
    def _parse_models(cls, raw: str) -> ParsedModels:
        """Map the model-list endpoint's JSON body into :class:`ParsedModels`.
        A payload offering nothing is a tagged error, never an ok-empty."""
        return ParsedModels(ok=False, error="unsupported")


def _terminal_detail(result: HarnessRunResult) -> str:
    """The CLI's terminal error ("You've hit your session limit · resets
    5:10pm") rides the stream's last result event; without it the persisted
    failure is a bare exit code and the operator has to dig transcripts.
    A CLI that died before emitting events leaves its reason on stderr,
    so the last non-empty line stands in."""
    for line in reversed(result.stdout.splitlines()[-20:]):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("is_error") and isinstance(event.get("result"), str):
            return f" {event['result'][:300]}"
        message = event.get("message")
        if event.get("type") == "error" and isinstance(message, str) and message:
            return f" {message[:300]}"
        error = event.get("error")
        if isinstance(error, dict):
            error = error.get("message")
        if isinstance(error, str) and error:
            return f" {error[:300]}"
    for line in reversed(result.stderr.decode("utf-8", errors="replace").splitlines()):
        if detail := line.strip():
            return f" {detail[:300]}"
    return ""
