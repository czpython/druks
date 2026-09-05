import contextlib
import json
import logging
import shlex
from pathlib import Path
from typing import Any

from druks.sandbox.datastructures import (
    AgentInvocation,
    Credentials,
    HarnessRunResult,
    HomeCopy,
    HomeFile,
    McpServer,
)
from druks.sandbox.layout import get_runs_root
from druks.skills.models import Skill

from . import exceptions
from .artifacts import call_dir, write_cost
from .base import Harness
from .constants import CLAUDE_DISALLOWED_TOOLS
from .datastructures import SandboxSettings
from .models import ProviderSubscription
from .providers import AnthropicProvider

logger = logging.getLogger(__name__)


class ClaudeHarness(Harness):
    # Claude streams its rollout as JSONL on stdout
    # (``--output-format stream-json``), so the transcript is the stdout.
    name = "claude"
    provider = AnthropicProvider.id
    billing_options = frozenset({"subscription", "api_key"})
    default_model = "anthropic/claude-opus-4-7"
    command = "claude"

    # The CLI dies with the raw API error in the result text ("API Error: 529
    # {…overloaded_error…}"), so "api error: 5" covers 529 and every 5xx; 429
    # and 401 are their own families.
    failure_markers = {
        "session limit": exceptions.HarnessUsageLimitError,
        "usage limit": exceptions.HarnessUsageLimitError,
        "api error: 429": exceptions.HarnessRateLimitError,
        "rate limit": exceptions.HarnessRateLimitError,
        "rate_limit": exceptions.HarnessRateLimitError,
        "api error: 401": exceptions.HarnessAuthError,
        "authentication_error": exceptions.HarnessAuthError,
        "failed to authenticate": exceptions.HarnessAuthError,
        "oauth token": exceptions.HarnessAuthError,
        "invalid api key": exceptions.HarnessAuthError,
        "overloaded": exceptions.HarnessOverloadedError,
        "api error: 5": exceptions.HarnessOverloadedError,
    }

    async def build_invocation(
        self,
        *,
        prompt: str,
        schema: dict[str, object],
        run_id: str,
        ssh_username: str,
        github_token: str | None = None,
        include_plugins: bool = True,
        add_dirs: tuple[str, ...] = (),
        skills: tuple[str, ...] = (),
        extra_env: dict[str, str] | None = None,
        mcp_servers: tuple[McpServer, ...] = (),
        subscription: ProviderSubscription | None = None,
        key: str | None = None,
        timeout: int = Harness.default_timeout,
    ) -> AgentInvocation:
        if not self.sandbox:
            raise exceptions.HarnessError(
                "claude harness requires sandbox settings — set sandbox.service_url and "
                "related TOML settings.",
            )

        in_vm_run_dir = f"{get_runs_root(ssh_username)}/{run_id}"
        in_vm_debug = f"{in_vm_run_dir}/debug.log"
        in_vm_session = f"{in_vm_run_dir}/session.jsonl"
        # Prompt rides as stdin (SFTP-uploaded) to avoid the SSH exec
        # channel's per-request size limit.
        claude_argv = [
            *self._command_args(),
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--json-schema",
            json.dumps(schema),
            "--permission-mode",
            "bypassPermissions",
            "--debug-file",
            in_vm_debug,
            "--disallowedTools",
            *CLAUDE_DISALLOWED_TOOLS,
        ]
        # Grant tool access to the related-repo clones in the VM. Claude scopes
        # file tools to cwd + --add-dir; without these it can't read the
        # siblings even though the prompt names their paths.
        for extra_dir in add_dirs:
            claude_argv += ["--add-dir", extra_dir]
        claude_argv += self._mcp_flags(mcp_servers)
        # Wrap so we can snapshot the per-invocation session JSONL that
        # claude writes under ``~/.claude/projects/<cwd-hash>/`` — the
        # codex-equivalent of CODEX_HOME/sessions. Sentinel + ``-newer``
        # picks the right one when prior calls left files behind.
        claude_cmdline = " ".join(shlex.quote(a) for a in claude_argv)
        run_dir_q = shlex.quote(in_vm_run_dir)
        session_q = shlex.quote(in_vm_session)
        wrapper = (
            f"mkdir -p {run_dir_q} && "
            f"touch {run_dir_q}/.start && "
            f"{claude_cmdline}; "
            "ec=$?; "
            f"sf=$(find $HOME/.claude/projects -name '*.jsonl' -type f "
            f"-newer {run_dir_q}/.start 2>/dev/null | head -1); "
            f'if [ -n "$sf" ]; then cp "$sf" {session_q}; fi; '
            "exit $ec"
        )
        env = dict(extra_env or {})
        if key:
            env["ANTHROPIC_API_KEY"] = key
        return AgentInvocation(
            name="claude",
            args=("sh", "-c", wrapper),
            stdin=prompt.encode("utf-8"),
            credentials=await _get_credentials(
                self.sandbox,
                github_token=github_token,
                include_plugins=include_plugins,
                skills=skills,
                subscription=subscription,
            ),
            env=env,
            extra_artifact_filenames=("debug.log", "session.jsonl"),
        )

    def parse(self, result: HarnessRunResult, *, artifact_dir: Path, run_id: str) -> Any:
        self.check_returncode(result)

        try:
            envelope = collapse_claude_stream(result.stdout)
        except exceptions.StreamJsonError as error:
            transcript = artifact_dir / run_id / "stdout.jsonl"
            raise exceptions.HarnessInvalidOutputError(
                f"claude wrote invalid stream-json. See {transcript}",
            ) from error

        cost_usd, cost_metadata = extract_claude_cost_from_envelope(envelope)
        write_cost(artifact_dir / run_id, cost_usd=cost_usd, metadata=cost_metadata)

        structured: Any = envelope.get("structured_output")
        if structured is None:
            structured = envelope.get("result")
        if isinstance(structured, str):
            with contextlib.suppress(json.JSONDecodeError):
                structured = json.loads(structured)

        output_path = call_dir(artifact_dir, run_id) / "output.json"
        if isinstance(structured, str):
            output_path.write_text(structured)
        else:
            output_path.write_text(
                json.dumps(structured, indent=2, sort_keys=True, default=str),
            )
        return structured

    def _mcp_flags(self, servers: tuple[McpServer, ...]) -> tuple[str, ...]:
        """``--mcp-config`` JSON registering each MCP server for this run.
        Claude expands ``${VAR}`` refs in header values from the run env at
        connect time, so no secret — the bearer or a secret header — ever
        lands in the emitted config; only non-secret values ride inline."""
        if not servers:
            return ()
        entries = {}
        for server in servers:
            headers = dict(server.headers)
            if server.bearer_token_env_var:
                headers["Authorization"] = f"Bearer ${{{server.bearer_token_env_var}}}"
            for header, env_var in server.env_headers.items():
                headers[header] = f"${{{env_var}}}"
            entry: dict[str, object] = {"type": "http", "url": server.url}
            if headers:
                entry["headers"] = headers
            entries[server.name] = entry
        return ("--mcp-config", json.dumps({"mcpServers": entries}))

    @classmethod
    def auth_file(cls, subscription: ProviderSubscription) -> HomeFile:
        return HomeFile(".claude/.credentials.json", json.dumps(dict(subscription.payload)))

    def _command_args(self) -> tuple[str, ...]:
        args = (self.command,)
        if self.model:
            args = (*args, "--model", self.model_id)
        if self.fast_mode:
            args = (*args, "--settings", json.dumps({"fastMode": True}))
        if self.effort:
            args = (*args, "--effort", self.effort)
        return args


async def _get_credentials(
    sandbox: SandboxSettings,
    *,
    github_token: str | None,
    include_plugins: bool = True,
    skills: tuple[str, ...] = (),
    subscription: ProviderSubscription | None,
) -> Credentials:
    """The Credentials bundle the runner pushes into the sandbox: the rendered
    credentials file, plus any local config, plugins, and skills.
    ``include_plugins=False`` skips the operator's plugin state, for prompts
    that use no MCP server and would otherwise die on a misconfigured plugin."""
    config_dir = sandbox.harness_config_root / ClaudeHarness.name
    home: list[HomeFile | HomeCopy] = []
    if subscription:
        home.append(ClaudeHarness.auth_file(subscription))
    home += [
        HomeCopy(".claude.json", config_dir / ".claude.json"),
        HomeCopy(".claude/settings.json", config_dir / "settings.json"),
        HomeCopy(".claude/CLAUDE.md", config_dir / "CLAUDE.md"),
    ]
    if include_plugins:
        plugins = config_dir / "plugins"
        home += [
            HomeCopy(".claude/plugins/installed_plugins.json", plugins / "installed_plugins.json"),
            HomeCopy(
                ".claude/plugins/known_marketplaces.json", plugins / "known_marketplaces.json"
            ),
            HomeCopy(".claude/plugins/marketplaces", plugins / "marketplaces"),
            HomeCopy(".claude/plugins/cache", plugins / "cache"),
        ]
    skills_dir = sandbox.skills_dir or config_dir / "skills"
    home.append(
        HomeCopy(".claude/skills", skills_dir, excludes=await Skill.delivery_excludes(skills))
    )
    return Credentials(home=tuple(home), github_token=github_token)


def collapse_claude_stream(stdout: bytes) -> dict[str, Any]:
    envelope: dict[str, Any] = {}
    result_seen = False
    parsed_any = False
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        parsed_any = True
        event_type = event.get("type")
        if event_type == "system" and event.get("subtype") == "init":
            model = event.get("model")
            if isinstance(model, str):
                envelope["model"] = model
        elif event_type == "result":
            for key, value in event.items():
                if key == "type":
                    continue
                envelope[key] = value
            result_seen = True
    if not parsed_any:
        raise exceptions.StreamJsonError("claude stream-json contained no parseable events")
    if not result_seen:
        raise exceptions.StreamJsonError("claude stream-json had no 'result' event")
    return envelope


def extract_claude_cost_from_envelope(
    envelope: dict[str, Any],
) -> tuple[float | None, dict[str, Any] | None]:
    cost = envelope.get("total_cost_usd")
    usage = envelope.get("usage")
    cost_usd: float | None = None
    if isinstance(cost, int | float):
        cost_usd = float(cost)
    metadata: dict[str, Any] = {"provider": "anthropic"}
    if isinstance(usage, dict):
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            value = usage.get(key)
            if isinstance(value, int):
                metadata[key] = value
    model = envelope.get("model")
    if isinstance(model, str):
        metadata["model"] = model
    duration_ms = envelope.get("duration_ms")
    if isinstance(duration_ms, int | float):
        metadata["duration_ms"] = duration_ms
    return cost_usd, metadata if (cost_usd is not None or len(metadata) > 1) else None
