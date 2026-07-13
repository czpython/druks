import json
import logging
import os
import secrets
import shlex
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from druks.sandbox.datastructures import (
    AgentInvocation,
    Credentials,
    HarnessRunResult,
    McpServer,
)
from druks.sandbox.layout import get_runs_root, get_work_root
from druks.skills.models import Skill

from .artifacts import write_cost
from .base import Harness, check_returncode, jwt_claims, jwt_expiry, post_token
from .datastructures import CodexToken, ParsedMetric, ParsedUsage
from .exceptions import HarnessError, OAuthTokenError
from .subprocess import read_result_json

logger = logging.getLogger(__name__)


# Namespaced claims OpenAI packs into the Codex access-token JWT.
_OPENAI_AUTH_CLAIM = "https://api.openai.com/auth"
_OPENAI_PROFILE_CLAIM = "https://api.openai.com/profile"


_TOKEN_COUNT_MARKERS = ('"type":"token_count"', '"type": "token_count"')

# Codex (ChatGPT subscription) usage endpoint — the standalone fetch the
# `codex` CLI's account/rateLimits/read RPC uses for the `chatgpt` auth
# extension. Returns the same numbers /status shows without a completion.
_CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
_CODEX_USER_AGENT = "codex-cli"


@dataclass(frozen=True)
class _Usage:
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class _Rate:
    """USD per million tokens."""

    input: float
    cached_input: float
    output: float


# Public OpenAI/Codex rates as of the last update. Override via env when prices
# change. Cached-input defaults to ~10% of input (OpenAI's typical discount).
_DEFAULT_RATES: dict[str, _Rate] = {
    # gpt-5.5 is the only Codex model callable on a ChatGPT-subscription
    # auth (which is how we run). Assumed-equal to gpt-5's rate until
    # OpenAI publishes specific gpt-5.5 pricing — override via
    # DRUKS_CODEX_PRICES_JSON when that lands.
    "gpt-5.5": _Rate(input=1.25, cached_input=0.125, output=10.0),
    "gpt-5": _Rate(input=1.25, cached_input=0.125, output=10.0),
    "gpt-5-codex": _Rate(input=1.25, cached_input=0.125, output=10.0),
    "gpt-5-mini": _Rate(input=0.25, cached_input=0.025, output=2.0),
    "gpt-5-nano": _Rate(input=0.05, cached_input=0.005, output=0.40),
    "o3-mini": _Rate(input=1.10, cached_input=0.55, output=4.40),
    "o4-mini": _Rate(input=1.10, cached_input=0.55, output=4.40),
}
_DEFAULT_RATE = _Rate(input=1.25, cached_input=0.125, output=10.0)


def read_codex_cost_from_jsonl(
    path: Path,
    *,
    model: str | None,
) -> tuple[float | None, dict[str, Any] | None]:
    if not path.exists():
        return None, None

    # The wide-open window makes the events-in-window filter accept every
    # event; the downloaded file is by construction the right one.
    epoch = datetime.min.replace(tzinfo=UTC)
    horizon = datetime.max.replace(tzinfo=UTC)
    events = list(_parse_token_count_events(path, epoch, horizon))
    if not events:
        return None, None

    latest = max(events, key=lambda event: event["timestamp"])
    usage: _Usage = latest["usage"]
    event_model = latest.get("model") or model
    rate, used_default = _rate_for(event_model)

    uncached_input = max(usage.input_tokens - usage.cached_input_tokens, 0)
    billable_output = usage.output_tokens + usage.reasoning_output_tokens
    cost = (
        uncached_input * rate.input
        + usage.cached_input_tokens * rate.cached_input
        + billable_output * rate.output
    ) / 1_000_000

    metadata: dict[str, Any] = {
        "provider": "openai",
        "model": event_model or "unknown",
        "input_tokens": usage.input_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "output_tokens": usage.output_tokens,
        "reasoning_output_tokens": usage.reasoning_output_tokens,
        "total_tokens": usage.total_tokens,
        "source": "codex_session_jsonl_sandboxed",
        "input_per_million_usd": rate.input,
        "cached_input_per_million_usd": rate.cached_input,
        "output_per_million_usd": rate.output,
    }
    if used_default:
        metadata["note"] = (
            "fell back to default rate; configure DRUKS_CODEX_PRICES_JSON to override"
        )
    return round(cost, 6), metadata


def _with_final_message_note(prompt: str) -> str:
    """Append the messaging contract to the prompt. --output-schema forces
    every assistant message into the schema shape, so interim messages are
    unreadable noise — narration belongs in the reasoning channel, and the
    run should emit exactly one assistant message: the final result."""
    return (
        f"{prompt}\n\n"
        "## Messaging\n\n"
        "Do not send interim assistant messages — narrate your work in your "
        "reasoning instead. Send exactly one assistant message: the final "
        "result object."
    )


def _parse_token_count_events(
    path: Path,
    window_start: datetime,
    window_end: datetime,
) -> Iterator[dict[str, Any]]:
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            if not any(marker in line for marker in _TOKEN_COUNT_MARKERS):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            timestamp_raw = obj.get("timestamp")
            if not isinstance(timestamp_raw, str):
                continue
            try:
                ts = datetime.fromisoformat(timestamp_raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if not (window_start <= ts <= window_end):
                continue

            # Newer codex CLIs wrap events as
            # ``{"type":"event_msg","payload":{"type":"token_count","info":{...}}}``;
            # older builds had ``info`` at the top level. Accept both.
            payload = obj.get("payload")
            info = payload.get("info") if isinstance(payload, dict) else obj.get("info")
            if not isinstance(info, dict):
                continue
            usage_raw = info.get("total_token_usage") or info.get("last_token_usage")
            if not isinstance(usage_raw, dict):
                continue
            usage = _normalize_usage(usage_raw)
            if usage is None:
                continue
            model = info.get("model") or info.get("model_name")
            yield {
                "timestamp": ts,
                "usage": usage,
                "model": model if isinstance(model, str) else None,
            }


def _normalize_usage(raw: dict[str, Any]) -> _Usage | None:
    def _num(value: Any) -> int:
        # ``bool`` is a subclass of ``int`` in Python; exclude it explicitly so
        # a stray ``true`` in the JSONL doesn't coerce to 1.
        if isinstance(value, bool):
            return 0
        return int(value) if isinstance(value, int | float) else 0

    input_tokens = _num(raw.get("input_tokens"))
    cached_input = _num(
        raw.get("cached_input_tokens")
        if "cached_input_tokens" in raw
        else raw.get("cache_read_input_tokens", 0)
    )
    output_tokens = _num(raw.get("output_tokens"))
    reasoning = _num(raw.get("reasoning_output_tokens"))
    total = _num(raw.get("total_tokens"))
    if total <= 0:
        total = input_tokens + output_tokens + reasoning
    return _Usage(
        input_tokens=input_tokens,
        cached_input_tokens=cached_input,
        output_tokens=output_tokens,
        reasoning_output_tokens=reasoning,
        total_tokens=total,
    )


def _rate_for(model: str | None) -> tuple[_Rate, bool]:
    if not model:
        return _DEFAULT_RATE, True
    overrides = _load_overrides()
    if model in overrides:
        return overrides[model], False
    if model in _DEFAULT_RATES:
        return _DEFAULT_RATES[model], False
    # Prefix-match Codex variants (e.g. ``gpt-5-codex-preview-2026-05-01``).
    for known, rate in _DEFAULT_RATES.items():
        if model.startswith(known):
            return rate, False
    return _DEFAULT_RATE, True


@lru_cache(maxsize=1)
def _load_overrides() -> dict[str, _Rate]:
    path = os.environ.get("DRUKS_CODEX_PRICES_JSON")
    if not path:
        return {}
    try:
        raw = json.loads(Path(path).expanduser().read_text())
    except (OSError, json.JSONDecodeError):
        logger.warning("Could not load Codex price overrides from %s", path, exc_info=True)
        return {}
    if not isinstance(raw, dict):
        return {}
    rates: dict[str, _Rate] = {}
    for model, entry in raw.items():
        if not isinstance(model, str) or not isinstance(entry, dict):
            continue
        try:
            input_rate = float(entry["input"])
            output_rate = float(entry["output"])
        except (KeyError, TypeError, ValueError):
            logger.warning("Skipping malformed Codex price entry: %s", model)
            continue
        cached_rate = entry.get("cached_input", input_rate * 0.1)
        try:
            cached_rate = float(cached_rate)
        except (TypeError, ValueError):
            cached_rate = input_rate * 0.1
        rates[model] = _Rate(input=input_rate, cached_input=cached_rate, output=output_rate)
    return rates


class CodexHarness(Harness):
    # With ``--json``, codex streams its rollout (reasoning, tool calls,
    # messages) to stdout as it runs — so stdout.jsonl is the live transcript,
    # symmetric with claude. session.jsonl is still snapshotted for cost.
    name = "codex"
    provider = "openai"
    model_prefixes = ("gpt-", "o1", "o3", "o4")
    models = ("gpt-5.5",)
    default_model = "gpt-5.5"
    command = "codex"

    # OAuth refresh config (consumed by the Harness templates).
    REFRESH_MARGIN = timedelta(hours=24)
    _TOKEN_URL = "https://auth.openai.com/oauth/token"
    _CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

    # Connect-flow (PKCE): authorize on auth.openai.com; the operator pastes the
    # failed localhost redirect URL back. Verified in ENG-687.
    redirect_uri = "http://localhost:1455/auth/callback"

    @classmethod
    def _token_from_credentials(cls, data: dict) -> CodexToken:
        tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
        access = tokens.get("access_token")
        if not access:
            raise OAuthTokenError("no_token", "codex auth file has no access token")
        return CodexToken(
            access_token=access,
            expires_at=jwt_expiry(access),
            account_id=tokens.get("account_id"),
        )

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
            "originator": "pi",  # the value verified in ENG-687; others untested
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
        access = grant.get("access_token")
        if not access:
            raise ValueError("refresh response had no access_token")
        tokens = data["tokens"]
        tokens["access_token"] = access

        if grant.get("refresh_token"):
            tokens["refresh_token"] = grant["refresh_token"]
        if grant.get("id_token"):
            tokens["id_token"] = grant["id_token"]
        data["last_refresh"] = now.astimezone(UTC).isoformat().replace("+00:00", "Z")
        return jwt_expiry(access)

    @classmethod
    def _usage_request(cls, token: CodexToken) -> tuple[str, dict]:
        headers = {
            "Authorization": f"Bearer {token.access_token}",
            "User-Agent": _CODEX_USER_AGENT,
        }
        if token.account_id:
            headers["ChatGPT-Account-Id"] = token.account_id
        return _CODEX_USAGE_URL, headers

    @classmethod
    def _parse_usage(cls, raw: str) -> ParsedUsage:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return ParsedUsage(ok=False, error="unparseable", raw=raw)
        if not isinstance(data, dict) or "rate_limit" not in data:
            return ParsedUsage(ok=False, error="unexpected_payload", raw=raw)
        rate_limit = data.get("rate_limit") or {}
        plan = data.get("plan_type") if isinstance(data.get("plan_type"), str) else None
        five_hour = _codex_window(rate_limit.get("primary_window"))
        week = _codex_window(rate_limit.get("secondary_window"))
        if five_hour is None and week is None:
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
                    week=full,
                    unlimited=True,
                    raw=raw,
                )
            return ParsedUsage(ok=False, error="parse_failed", plan_tier=plan, raw=raw)
        return ParsedUsage(
            ok=True,
            plan_tier=plan,
            five_hour=five_hour,
            week=week,
            raw=raw,
        )

    def _build_codex_wrapper(
        self,
        *,
        ssh_username: str,
        schema: dict[str, object],
        run_id: str,
        codex_flags: tuple[str, ...],
        cwd: str,
    ) -> list[str]:
        # In-VM paths under <get_runs_root>/<run_id>/. Schema is inlined via
        # printf (~few KB); the prompt rides as stdin via the helper.
        in_vm_run_dir = f"{get_runs_root(ssh_username)}/{run_id}"
        in_vm_schema = f"{in_vm_run_dir}/schema.json"
        in_vm_output = f"{in_vm_run_dir}/output.json"
        in_vm_marker = f"{in_vm_run_dir}/session.marker"
        in_vm_session = f"{in_vm_run_dir}/session.jsonl"

        # Callers own schema validity: the API behind --output-schema
        # enforces OpenAI's strict rules (additionalProperties: false +
        # all-keys required on every object node). Pydantic-derived
        # schemas get there via the SDK's AgentOutput base (extra="forbid"
        # + every field required); build's contracts subclass it. A
        # non-strict schema fails loudly with a 400 naming the offending
        # node.
        schema_body = json.dumps(schema, indent=2, sort_keys=True)

        # The codex CLI invocation with all flags rewired to in-VM
        # paths. ``--cd`` points at the in-VM working dir; ``-`` tells
        # codex to read the prompt from stdin — the helper script
        # redirects stdin from the SFTP-uploaded prompt file via
        # ``--stdin-from``, so no explicit redirect here.
        codex_argv = [
            *codex_flags,
            "-",
            "--cd",
            cwd,
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "--output-schema",
            in_vm_schema,
            "--output-last-message",
            in_vm_output,
        ]
        codex_cmdline = " ".join(shlex.quote(a) for a in codex_argv)
        marker_q = shlex.quote(in_vm_marker)
        session_q = shlex.quote(in_vm_session)
        # Codex runs against its real ``~/.codex`` because CODEX_HOME re-homes
        # all home-resolved state: auth, skills, and future features. A marker
        # file plus ``-newer`` identifies the session JSONL. More than one match
        # warns on stderr and copies nothing rather than mis-attributing cost;
        # a missing session degrades to cost=None.
        wrapper = (
            f"mkdir -p {shlex.quote(in_vm_run_dir)} && "
            f"printf %s {shlex.quote(schema_body)} > {shlex.quote(in_vm_schema)} && "
            f"touch {marker_q} && "
            f"{codex_cmdline}; "
            "ec=$?; "
            f"sessions=$(find \"$HOME/.codex/sessions\" -type f -name '*.jsonl' "
            f"-newer {marker_q} 2>/dev/null); "
            "count=$(printf '%s\\n' \"$sessions\" | grep -c .); "
            f'if [ "$count" -eq 1 ]; then cp "$sessions" {session_q}; '
            'elif [ "$count" -gt 1 ]; then '
            'echo "codex wrapper: $count session files newer than the marker;'
            ' refusing to guess which belongs to this run" >&2; fi; '
            "exit $ec"
        )

        return ["sh", "-c", wrapper]

    def build_invocation(
        self,
        *,
        prompt: str,
        schema: dict[str, object],
        run_id: str,
        ssh_username: str,
        github_token: str | None = None,
        # Accepted for signature parity with ClaudeHarness; codex has no
        # plugin layer so there's nothing to skip, and it runs with full FS
        # access so it needs no per-dir grants.
        include_plugins: bool = True,
        add_dirs: tuple[str, ...] = (),
        extra_env: dict[str, str] | None = None,
        mcp_servers: tuple[McpServer, ...] = (),
    ) -> AgentInvocation:
        if not self.sandbox:
            raise HarnessError(
                f"{self.name} harness requires sandbox settings — set "
                "DRUKS_SANDBOX_SERVICE_URL et al.",
            )

        cmd = self._build_codex_wrapper(
            ssh_username=ssh_username,
            schema=schema,
            run_id=run_id,
            codex_flags=(*self._prompt_flags(), *self._mcp_flags(mcp_servers)),
            cwd=get_work_root(ssh_username),
        )
        # --output-schema constrains EVERY agent_message mechanically (the
        # "final response shape" in its docs is inaccurate — verified by
        # probing: an explicitly requested plain-text interim message came
        # back schema-shaped). So interim messages can only ever be JSON
        # noise; the prompt note suppresses them instead, keeping the hard
        # validity guarantee for the one message that matters.
        return AgentInvocation(
            name=self.name,
            args=tuple(cmd),
            stdin=_with_final_message_note(prompt).encode("utf-8"),
            credentials=self._codex_credentials(github_token=github_token),
            env=extra_env,
            extra_artifact_filenames=("output.json", "session.jsonl"),
        )

    def parse(self, result: HarnessRunResult, *, artifact_dir: Path, run_id: str) -> Any:
        check_returncode(result, name=self.name)

        call_dir = artifact_dir / run_id
        payload = read_result_json(
            call_dir / "output.json",
            name=self.name,
        )
        (call_dir / "output.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True),
        )
        cost_usd, cost_metadata = read_codex_cost_from_jsonl(
            call_dir / "session.jsonl",
            model=self.model,
        )
        write_cost(call_dir, cost_usd=cost_usd, metadata=cost_metadata)
        return payload

    def _mcp_flags(self, servers: tuple[McpServer, ...]) -> tuple[str, ...]:
        """Inline ``-c`` overrides registering each MCP server on this run —
        no persisted config, so it survives the per-run config push. Secrets
        stay out of argv: codex reads the bearer and every env_http_headers
        value from the named env var at runtime; only non-secret header values
        ride inline. A header name is a quoted TOML key segment — a valid HTTP
        header name carries no quote, so plain quoting holds."""
        flags: tuple[str, ...] = ()
        for server in servers:
            flags = (*flags, "-c", f'mcp_servers.{server.name}.url="{server.url}"')
            if server.bearer_token_env_var:
                flags = (
                    *flags,
                    "-c",
                    f"mcp_servers.{server.name}.bearer_token_env_var="
                    f'"{server.bearer_token_env_var}"',
                )
            for header, value in server.headers.items():
                flags = (
                    *flags,
                    "-c",
                    f'mcp_servers.{server.name}.http_headers."{header}"="{value}"',
                )
            for header, env_var in server.env_headers.items():
                flags = (
                    *flags,
                    "-c",
                    f'mcp_servers.{server.name}.env_http_headers."{header}"="{env_var}"',
                )
        return flags

    def _prompt_flags(self) -> tuple[str, ...]:
        args = (self.command, "exec")

        if self.model:
            args = (*args, "--model", self.model)
        if self.fast_mode:
            args = (*args, "-c", "features.fast_mode=true", "-c", 'service_tier="fast"')
        # Emit a human-readable reasoning summary. Codex encrypts the raw
        # reasoning, so the summary is the only readable form; without this the
        # reasoning items arrive with an empty ``summary`` and the transcript
        # shows nothing for them.
        args = (*args, "-c", "model_reasoning_summary=auto")
        if self.effort:
            args = (*args, "-c", f"model_reasoning_effort={self.effort}")
        # Stream one JSONL event per line on stdout (reasoning chunks, tool
        # calls, messages) so the transcript tails live. The structured result
        # still lands in the --output-last-message file, untouched.
        args = (*args, "--json")
        return args

    def _codex_credentials(self, *, github_token: str | None) -> Credentials:
        # The credential file is synthesized from the DB row (raises when codex
        # isn't connected); the local config dir only adds config carry on top.
        assert self.sandbox is not None  # callers guard
        config_dir = self.sandbox.codex_config_dir
        files: tuple[tuple[Path, str], ...] = ()
        if config_dir:
            files = (
                (config_dir / "config.toml", ".codex/config.toml"),
                (config_dir / ".credentials.json", ".codex/.credentials.json"),
                (config_dir / "AGENTS.md", ".codex/AGENTS.md"),
            )
        # Skills from the canonical shared dir (DRUKS_SKILLS_DIR) when set — the
        # same set pushed to ~/.claude/skills — else the per-CLI fallback. Must
        # be real dirs (tar follows symlinks).
        skills_src = self.sandbox.skills_dir or (config_dir / "skills" if config_dir else None)
        dirs: tuple[tuple[Path, str], ...] = ()
        if skills_src:
            dirs = ((skills_src, ".codex/skills"),)
        return Credentials(
            codex_credentials=self.render_credentials_file(),
            github_token=github_token,
            extra_config_files=files,
            extra_config_dirs=dirs,
            extra_dir_excludes={".codex/skills": Skill.disabled_excludes()},
        )


def _codex_window(block: object) -> ParsedMetric | None:
    if not isinstance(block, dict):
        return None
    used = block.get("used_percent")
    percent_left = None
    if isinstance(used, (int, float)):
        percent_left = max(0, min(100, int(round(100 - used))))
    resets_at = _codex_reset(block.get("reset_at"))
    if percent_left is None and resets_at is None:
        return None
    return ParsedMetric(percent_left=percent_left, resets_at=resets_at)


def _codex_reset(value: object) -> datetime | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
