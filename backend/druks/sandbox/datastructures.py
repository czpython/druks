import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from druks.database import db_session
from druks.mcp import models as mcp_models
from druks.mcp import oauth
from druks.mcp.constants import TOKEN_ENV_PREFIX
from druks.mcp.enums import TokenSource
from druks.mcp.exceptions import MissingTokenError, SourceEnvVarUnsetError
from druks.mcp.helpers import get_bearer_token_env_var

if TYPE_CHECKING:
    from druks.durable.enums import AgentCallStatus

    from .host import Sandbox


class Profile(BaseModel):
    """A repo's VM image + env, from ``.druks/<extension>/config.yml``."""

    model_config = {"frozen": True, "extra": "forbid"}

    image: str | None = None
    env: dict[str, str] = Field(default_factory=dict)


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
    """Result of one agent execution, what ``Sandbox.run_agent`` returns.
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
    last_error: str | None = None


@dataclass
class HarnessRunResult:
    """Raw outcome of one CLI execution in the VM — what ``Sandbox._exec``
    returns and a harness's ``parse`` consumes."""

    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True)
class AgentInvocation:
    """A fully-built CLI invocation, ready for ``Sandbox._exec``.

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
    a run-scoped token the operator registry can't hold (build's per-repo
    reviewer token). It owns its name: a same-named registry entry is not
    delivered."""

    name: str
    url: str
    token: str = field(repr=False)


@dataclass(frozen=True)
class Workspace:
    # What an agent runs in: the VM it abstracts. An extension subclasses this and
    # overrides get_agent_run_kwargs for its project scaffolding (repo token, dirs)
    # and get_required_mcp_servers for MCP servers it credentials itself.
    sandbox: "Sandbox"

    @property
    def host_id(self) -> str:
        return self.sandbox.id

    def get_agent_run_kwargs(self, **kwargs: Any) -> dict[str, Any]:
        # Override to add what the agent's run needs on this workspace (github_token,
        # add_dirs). Base: pass the run's kwargs through untouched.
        return kwargs

    def get_required_mcp_servers(self) -> tuple[RequiredMcpServer, ...]:
        # Override to declare the servers this workspace requires and
        # credentials itself. Base: none.
        return ()

    async def run_agent(self, **kwargs: Any) -> AgentResult:
        run_kwargs = await self.with_mcp_servers(**self.get_agent_run_kwargs(**kwargs))
        # with_mcp_servers is the run's last DB read; commit so the step's
        # connection isn't held idle through the minutes the agent runs.
        db_session().commit()
        return await self.sandbox.run_agent(**run_kwargs)

    async def with_mcp_servers(self, **kwargs: Any) -> dict[str, Any]:
        # Fold every MCP server into this call — the workspace's required
        # servers, then the operator registry's enabled entries. Each becomes a
        # wire shape on ``mcp_servers`` (url + derived env var, never the
        # token); each token rides ``extra_env`` under that var.
        required = self.get_required_mcp_servers()
        required_names = {server.name for server in required}
        if len(required_names) != len(required):
            # One config key per name in the emitted harness config — a dupe
            # would break the VM's config parse mid-run.
            raise ValueError(f"duplicate required MCP server names: {sorted(required_names)}")
        enabled = mcp_models.McpServer.list_enabled()
        if not required and not enabled:
            return kwargs
        # ``extra_env`` may be omitted or an explicit ``None`` (both valid for the
        # underlying run_agent); treat them the same so the merge never unpacks None.
        env = dict(kwargs.get("extra_env") or {})
        wire = []
        for server in required:
            env[get_bearer_token_env_var(server.name)] = server.token
            wire.append(
                McpServer(
                    name=server.name,
                    url=server.url,
                    bearer_token_env_var=get_bearer_token_env_var(server.name),
                )
            )
        for server in enabled:
            if server["name"] in required_names:
                # A required server owns its name: the registry twin is neither
                # resolved (no raise, no env clobber) nor delivered.
                continue
            # Per-strategy bearer resolution, loud when a server can't
            # authenticate — delivery never ships a header the harness
            # can't fill.
            source = server["token_source"]
            if not source:
                # No bearer; auth, if any, rides the declared headers below.
                token = ""
            elif source == TokenSource.STATIC:
                # A stored token is ciphertext everywhere else; decrypted only
                # here, entering the run env.
                if not server["token"]:
                    raise MissingTokenError(server["name"])
                token = server["token"].decrypt()
            elif source == TokenSource.STATIC_FROM_ENV:
                token = os.environ.get(server["source_env_var"], "")
                if not token:
                    raise SourceEnvVarUnsetError(server["name"], server["source_env_var"])
            else:  # oauth
                token = await oauth.mint_access_token(server["name"])
            bearer_token_env_var = ""
            if token:
                bearer_token_env_var = get_bearer_token_env_var(server["name"])
                env[bearer_token_env_var] = token
            env_headers = {}
            for index, (header, value) in enumerate(server["secret_headers"].items()):
                env_var = f"{TOKEN_ENV_PREFIX}{server['name'].upper()}_HEADER_{index}"
                env[env_var] = value
                env_headers[header] = env_var
            wire.append(
                McpServer(
                    name=server["name"],
                    url=server["url"],
                    bearer_token_env_var=bearer_token_env_var,
                    headers=dict(server["headers"]),
                    env_headers=env_headers,
                )
            )
        kwargs["mcp_servers"] = tuple(wire)
        if env:
            kwargs["extra_env"] = env
        return kwargs


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
