import hashlib
import json
import logging
import uuid
from abc import ABC, abstractmethod
from collections.abc import Collection
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from druks.mcp import models as mcp_models
from druks.mcp.helpers import get_bearer_token_env_var
from druks.skills.models import Skill

from . import exceptions
from .datastructures import (
    AgentInvocation,
    HarnessRunResult,
    SandboxSettings,
)
from .models import ProviderSubscription
from .providers import Provider

if TYPE_CHECKING:
    from druks.sandbox.datastructures import McpServer

logger = logging.getLogger(__name__)

# The capability manifest is a plain JSON dict written per AgentCall. Bump when
# the recorded shape changes so a reader can tell manifests apart across
# versions; the value is part of the hash, so a bump reshuffles the buckets.
MANIFEST_SCHEMA_VERSION = 2


class Harness(ABC):
    name: str
    command: ClassVar[str]
    # What this CLI runs on: "subscription", "api_key", or both.
    billing_options: ClassVar[frozenset[str]]
    # The one provider a vendor's CLI is bound to. None for a key-only CLI,
    # which runs whichever provider the model names.
    provider: ClassVar[str | None] = None
    # The seed for this harness's settings row, ``provider/model``.
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
    def accepts(cls, subscription: ProviderSubscription) -> bool:
        """Whether this CLI runs on the subscription ``subscription``."""
        bound = not cls.provider or cls.provider == subscription.provider
        return bound and "subscription" in cls.billing_options

    @classmethod
    def has_provider(cls, provider: Provider) -> bool:
        """The one this CLI is bound to, or any that issues a subscription kind it consumes."""
        if cls.provider:
            return cls.provider == provider.id
        return bool(cls.billing_options & provider.billing_options)

    @property
    def model_id(self) -> str:
        """The model as the CLI names it, without the provider namespace."""
        return self.model.partition("/")[2]

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
