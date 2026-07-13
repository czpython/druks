import contextlib
import json
import logging
import shlex
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from druks.core.utils.time import ensure_utc
from druks.sandbox.datastructures import (
    AgentInvocation,
    Credentials,
    HarnessRunResult,
    McpServer,
)
from druks.sandbox.layout import get_runs_root
from druks.skills.models import Skill

from .artifacts import call_dir, write_cost
from .base import Harness, check_returncode, parse_epoch_expiry, post_token
from .datastructures import OAuthToken, ParsedMetric, ParsedUsage, SandboxSettings
from .exceptions import HarnessError, OAuthTokenError, StreamJsonError

logger = logging.getLogger(__name__)


_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# Beta flag the Claude CLI sends for OAuth-scoped endpoints.
_OAUTH_BETA = "oauth-2025-04-20"
_ANTHROPIC_VERSION = "2023-06-01"


class ClaudeHarness(Harness):
    # Claude streams its rollout as JSONL on stdout
    # (``--output-format stream-json``), so the transcript is the stdout.
    name = "claude"
    provider = "anthropic"
    model_prefixes = ("claude-",)
    models = ("claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5")
    default_model = "claude-opus-4-7"
    command = "claude"

    # OAuth refresh config (consumed by the Harness templates).
    REFRESH_MARGIN = timedelta(hours=2)
    _TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
    # Public Claude-Code OAuth client id.
    _CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"

    # Connect-flow (PKCE code-paste): authorize on claude.ai, land on the
    # console.anthropic.com code page, exchange JSON with the state echoed in the
    # body. Verified in ENG-687.
    redirect_uri = "https://console.anthropic.com/oauth/code/callback"

    def build_invocation(
        self,
        *,
        prompt: str,
        schema: dict[str, object],
        run_id: str,
        ssh_username: str,
        github_token: str | None = None,
        include_plugins: bool = True,
        add_dirs: tuple[str, ...] = (),
        extra_env: dict[str, str] | None = None,
        mcp_servers: tuple[McpServer, ...] = (),
    ) -> AgentInvocation:
        if not self.sandbox:
            raise HarnessError(
                "claude harness requires sandbox settings — set DRUKS_SANDBOX_SERVICE_URL et al.",
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
        return AgentInvocation(
            name="claude",
            args=("sh", "-c", wrapper),
            stdin=prompt.encode("utf-8"),
            credentials=_claude_credentials(
                self.sandbox,
                github_token=github_token,
                include_plugins=include_plugins,
            ),
            env=extra_env,
            extra_artifact_filenames=("debug.log", "session.jsonl"),
        )

    def parse(self, result: HarnessRunResult, *, artifact_dir: Path, run_id: str) -> Any:
        check_returncode(result, name="claude")

        try:
            envelope = collapse_claude_stream(result.stdout)
        except StreamJsonError as error:
            transcript = artifact_dir / run_id / "stdout.jsonl"
            raise HarnessError(
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
        Claude expands ``${VAR}`` in the auth header from the run env at
        connect time, so the bearer token never lands in the emitted config."""
        if not servers:
            return ()
        config = {
            "mcpServers": {
                server.name: {
                    "type": "http",
                    "url": server.url,
                    "headers": {
                        "Authorization": f"Bearer ${{{server.bearer_token_env_var}}}",
                    },
                }
                for server in servers
            }
        }
        return ("--mcp-config", json.dumps(config))

    def _command_args(self) -> tuple[str, ...]:
        args = (self.command,)
        if self.model:
            args = (*args, "--model", self.model)
        if self.fast_mode:
            args = (*args, "--settings", json.dumps({"fastMode": True}))
        if self.effort:
            args = (*args, "--effort", self.effort)
        return args

    @classmethod
    def _token_from_credentials(cls, data: dict) -> OAuthToken:
        block = _oauth_block(data)
        access = block.get("accessToken") or block.get("access_token")
        if not access:
            raise OAuthTokenError("no_token", "credentials file has no access token")
        return OAuthToken(
            access_token=access,
            expires_at=parse_epoch_expiry(block.get("expiresAt")),
            scopes=tuple(block.get("scopes") or ()),
            subscription_type=block.get("subscriptionType"),
        )

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
        # state (verified in ENG-687), so that's what login_complete checks.
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
        block = data["claudeAiOauth"] if isinstance(data.get("claudeAiOauth"), dict) else data
        access = grant.get("access_token")
        if not access:
            raise ValueError("refresh response had no access_token")
        block["accessToken"] = access
        if grant.get("refresh_token"):
            block["refreshToken"] = grant["refresh_token"]
        expires_in = grant.get("expires_in")
        if isinstance(expires_in, (int, float)):
            new_expiry = now + timedelta(seconds=expires_in)
            block["expiresAt"] = int(new_expiry.timestamp() * 1000)
            return new_expiry
        return parse_epoch_expiry(block.get("expiresAt"))

    @classmethod
    def _usage_request(cls, token: OAuthToken) -> tuple[str, dict]:
        headers = {
            "Authorization": f"Bearer {token.access_token}",
            "anthropic-beta": _OAUTH_BETA,
            "anthropic-version": _ANTHROPIC_VERSION,
        }
        return _USAGE_URL, headers

    @classmethod
    def _parse_usage(cls, raw: str) -> ParsedUsage:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return ParsedUsage(ok=False, error="unparseable", raw=raw)
        if not isinstance(data, dict) or not any(k in data for k in ("five_hour", "seven_day")):
            return ParsedUsage(ok=False, error="unexpected_payload", raw=raw)
        return ParsedUsage(
            ok=True,
            five_hour=_claude_metric(data.get("five_hour")),
            week=_claude_metric(data.get("seven_day")),
            raw=raw,
        )


def _oauth_block(data: dict) -> dict:
    """Claude nests the OAuth fields under ``claudeAiOauth``; tolerate a
    flat shape too."""
    block = data.get("claudeAiOauth") if isinstance(data, dict) else None
    return block if isinstance(block, dict) else data


def _claude_metric(block: object) -> ParsedMetric | None:
    if not isinstance(block, dict):
        return None
    utilization = block.get("utilization")
    percent_left = None
    if isinstance(utilization, (int, float)):
        percent_left = max(0, min(100, int(round(100 - utilization))))
    resets_at = _parse_iso(block.get("resets_at"))
    if percent_left is None and resets_at is None:
        return None
    return ParsedMetric(percent_left=percent_left, resets_at=resets_at)


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ensure_utc(parsed)


def _claude_credentials(
    sandbox: SandboxSettings,
    *,
    github_token: str | None,
    include_plugins: bool = True,
) -> Credentials:
    """Build the Credentials bundle the runner SFTP-pushes into the sandbox.
    The credential file is synthesized from the DB row (raises when claude
    isn't connected); the local config dir only adds config carry on top.

    ``include_plugins=False`` skips uploading the operator's local plugin
    state — ``installed_plugins.json``, ``known_marketplaces.json``, and
    the ``plugins/marketplaces`` / ``plugins/cache`` dirs. Used by callers
    (currently scout) whose claude prompt doesn't talk to any MCP server
    and would otherwise crash on operator-side plugin misconfiguration —
    e.g. a github MCP plugin that requires ``GITHUB_PERSONAL_ACCESS_TOKEN``
    and dies mid-call when it isn't set. ``.claude.json``, ``settings.json``
    and the skills tree are kept either way; those aren't plugins.
    """
    config_dir = sandbox.claude_config_dir
    files: tuple[tuple[Path, str], ...] = ()
    dirs: tuple[tuple[Path, str], ...] = ()
    if config_dir:
        files = (
            (config_dir.parent / ".claude.json", ".claude.json"),
            (config_dir / "settings.json", ".claude/settings.json"),
        )
        if include_plugins:
            files += (
                (
                    config_dir / "plugins" / "installed_plugins.json",
                    ".claude/plugins/installed_plugins.json",
                ),
                (
                    config_dir / "plugins" / "known_marketplaces.json",
                    ".claude/plugins/known_marketplaces.json",
                ),
            )
            dirs = (
                (config_dir / "plugins" / "marketplaces", ".claude/plugins/marketplaces"),
                (config_dir / "plugins" / "cache", ".claude/plugins/cache"),
            )
    # Skills come from the canonical shared dir (DRUKS_SKILLS_DIR) when set;
    # otherwise fall back to the per-CLI skills subdir.
    skills_src = sandbox.skills_dir or (config_dir / "skills" if config_dir else None)
    if skills_src:
        dirs += ((skills_src, ".claude/skills"),)
    return Credentials(
        claude_credentials=ClaudeHarness.render_credentials_file(),
        github_token=github_token,
        extra_config_files=files,
        extra_config_dirs=dirs,
        extra_dir_excludes={".claude/skills": Skill.disabled_excludes()},
    )


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
        raise StreamJsonError("claude stream-json contained no parseable events")
    if not result_seen:
        raise StreamJsonError("claude stream-json had no 'result' event")
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
