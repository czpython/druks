import hashlib
import importlib.util
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from druks.apps import loader

from .constants import DEFAULT_DIR_EXCLUDES
from .exceptions import SetupScriptError

if TYPE_CHECKING:
    from druks.durable.enums import AgentCallStatus
    from druks.harnesses.exceptions import HarnessError

    from .host import Host


class Profile(BaseModel):
    """A repo's VM image + env, from ``.druks/<app>/config.yml``."""

    model_config = {"frozen": True, "extra": "forbid"}

    image: str | None = None
    env: dict[str, str] = Field(default_factory=dict)


if TYPE_CHECKING:
    from .host import Host


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
    def setup_script_hash(self) -> str:
        # Drukbox's template identity: sha256 of the script text. Base image and
        # provider are the other two columns of its unique key, resolved there.
        return hashlib.sha256(self.read_setup_script()).hexdigest()


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
    a run-scoped token the operator registry can't hold (Software Factory's per-repo
    reviewer token). It owns its name: a same-named registry entry is not
    delivered."""

    name: str
    url: str
    token: str = field(repr=False)


@dataclass(frozen=True)
class HomeFile:
    """Rendered content written at a home-relative path, never staged on the host."""

    path: str
    content: str

    async def push(self, host: "Host", home: str) -> None:
        await host.write_secret(secret=self.content, remote=f"{home}/{self.path}")


@dataclass(frozen=True)
class HomeCopy:
    """A host file or tree copied to a home-relative path. What the host lacks
    is skipped: config is optional, and the CLIs mint their own defaults."""

    path: str
    source: Path
    excludes: tuple[str, ...] = ()

    async def push(self, host: "Host", home: str) -> None:
        remote = f"{home}/{self.path}"
        # A Docker bind mount whose host file never existed leaves a directory
        # at a file's path; is_file() keeps that out.
        if self.source.is_file():
            await host.upload_file(local=self.source, remote=remote)
        elif self.source.is_dir():
            await host.upload_dir(
                local=self.source, remote=remote, excludes=DEFAULT_DIR_EXCLUDES + self.excludes
            )


@dataclass(frozen=True)
class Credentials:
    """What lands in the sandbox home before the CLI runs."""

    home: tuple[HomeFile | HomeCopy, ...] = ()
    github_token: str | None = None
