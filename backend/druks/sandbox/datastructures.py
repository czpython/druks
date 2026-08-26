import hashlib
import importlib.util
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from druks.apps import loader
from druks.settings import load_settings

from .exceptions import SetupScriptError

if TYPE_CHECKING:
    from druks.durable.enums import AgentCallStatus
    from druks.harnesses.exceptions import HarnessError


class Profile(BaseModel):
    """A repo's VM image + env, from ``.druks/<app>/config.yml``."""

    model_config = {"frozen": True, "extra": "forbid"}

    image: str | None = None
    env: dict[str, str] = Field(default_factory=dict)


def get_content_hash(base: str, script: bytes) -> str:
    content = base.encode("utf-8") + b"\0" + script
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class Sandbox:
    # Path of the setup script inside the declaring app's package, by
    # convention under ``sandboxes/``. The owning module is stamped when the
    # declaration is assigned on a workflow class.
    setup: str
    module: str = field(init=False, compare=False, default="")

    def __set_name__(self, owner: type, attr: str) -> None:
        object.__setattr__(self, "module", owner.__module__)

    def read_setup_script(self) -> bytes:
        if not self.module:
            raise SetupScriptError(
                f"sandbox setup {self.setup!r} is not declared on a workflow class"
            )
        app = loader.get_app(loader.resolve_workflow_app(self.module))
        spec = importlib.util.find_spec(app.package)
        if not spec or not spec.submodule_search_locations:
            raise SetupScriptError(
                f"sandbox setup {self.setup!r} cannot find app package {app.package!r}"
            )
        path = Path(spec.submodule_search_locations[0]) / self.setup
        try:
            return path.read_bytes()
        except OSError as error:
            raise SetupScriptError(
                f"sandbox setup {self.setup!r} cannot be read: {error}"
            ) from error

    @property
    def content_hash(self) -> str:
        return get_content_hash(load_settings().sandbox.image, self.read_setup_script())


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True)
class AgentResult:
    """Result of one agent execution, what ``Host.run_agent`` returns.
    the agent call records it as an ``AgentCall`` and parses ``output``."""

    output: Any
    run_id: str
    sandbox_host_id: str
    model: str
    # Which agent (registry id) this execution ran for — labels the failure and
    # the recorded AgentCall.
    agent: str
    status: "AgentCallStatus"
    started_at: datetime
    cost_usd: float | None = None
    cost_metadata: dict[str, Any] | None = None
    error: "HarnessError | None" = None

    @property
    def last_error(self) -> str | None:
        if self.error:
            return f"{self.agent}: {type(self.error).__name__}: {self.error}"
        return


@dataclass
class HarnessRunResult:
    """Raw outcome of one CLI execution in the VM — what ``Host._exec``
    returns and a harness's ``parse`` consumes."""

    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True)
class AgentInvocation:
    """A fully-built CLI invocation, ready for ``Host._exec``.

    Produced by a harness's ``build_invocation`` — the harness is a pure
    planner (argv in, parsed payload out) and never touches the live
    sandbox; the sandbox owns execution. ``env`` values ride the exec
    environment only — anything secret stays out of ``args``."""

    name: str
    args: tuple[str, ...]
    stdin: bytes
    credentials: "Credentials"
    env: dict[str, str] | None = None
    cwd: str | None = None
    extra_artifact_filenames: tuple[str, ...] = ()


@dataclass(frozen=True)
class McpServer:
    """A streamable-HTTP MCP server the agent talks to. Config-safe by
    construction: no secret value appears here — the bearer token and every
    secret header value ride the run's env, and this shape names only their
    env vars; the harness reads them at runtime. Only non-secret declared
    header values are carried inline."""

    name: str
    url: str
    # "" when the server carries no Authorization bearer — its auth, if any,
    # rides the declared headers.
    bearer_token_env_var: str = ""
    # Non-secret declared headers, emitted inline: header name -> value.
    headers: dict[str, str] = field(default_factory=dict)
    # Secret declared headers: header name -> the env var carrying its value.
    env_headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RequiredMcpServer:
    """An MCP server a workspace requires for its runs and credentials itself —
    a run-scoped token the operator registry can't hold (Ship's per-repo
    reviewer token). It owns its name: a same-named registry entry is not
    delivered."""

    name: str
    url: str
    token: str = field(repr=False)


@dataclass(frozen=True)
class Credentials:
    # The credential-file JSON each CLI reads, synthesized from the DB row at
    # push time (``Harness.render_credentials_file()``, which raises when that
    # harness isn't connected); None when this bundle doesn't carry that CLI —
    # a claude bundle ships no codex credential and vice versa. Written into
    # the VM as a secret, never a host-file copy.
    claude_credentials: str | None = None
    codex_credentials: str | None = None
    github_token: str | None = None
    # Extra config files to carry into the VM, as
    # ``(local_path, home_relative_dest)`` pairs. This is how the
    # agents' MCP / plugin config travels — e.g.
    # ``(~/.codex/config.toml, ".codex/config.toml")`` brings the
    # curated remote plugins (linear / notion / figma) along, and the
    # sibling ``.credentials.json`` carries their auth. Each is pushed
    # under the agent user's in-VM home (so ``.codex/config.toml`` ->
    # ``/home/<user>/.codex/config.toml``) and **skipped if the local
    # file is absent** — config is optional, missing it isn't fatal.
    extra_config_files: tuple[tuple[Path, str], ...] = ()
    # Directory trees to carry into the VM, same
    # ``(local_path, home_relative_dest)`` shape, copied recursively.
    # This is how Claude's managed-plugin trees travel
    # (``~/.claude/plugins/marketplaces`` + ``.../cache``) since those
    # are directories, not flat files. Skipped if the local dir is
    # absent.
    extra_config_dirs: tuple[tuple[Path, str], ...] = ()
    # Per-destination extra tar excludes, keyed by the same home-relative dest
    # as ``extra_config_dirs``, merged with ``DEFAULT_DIR_EXCLUDES`` at push.
    # The skills projection uses this to drop disabled skills from the upload.
    extra_dir_excludes: dict[str, tuple[str, ...]] = field(default_factory=dict)
