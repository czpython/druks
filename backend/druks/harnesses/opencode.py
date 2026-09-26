import json
import logging
import urllib.parse
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from druks.accounts.enums import AccountKind
from druks.sandbox.datastructures import (
    AgentInvocation,
    Credentials,
    HarnessRunResult,
    McpServer,
)
from druks.sandbox.layout import get_runs_root, get_work_root

from . import exceptions
from .artifacts import call_dir, write_cost
from .base import Harness

logger = logging.getLogger(__name__)

_ABORT_MARGIN_SECONDS = 5
_WRAPPER = (Path(__file__).parent / "opencode_wrapper.sh").read_text()
_ERROR_TYPES = {
    "ProviderAuthError": exceptions.HarnessNotConnectedError,
    "StructuredOutputError": exceptions.HarnessInvalidOutputError,
    "MessageAborted": exceptions.HarnessTimeoutError,
}


class OpenCodeHarness(Harness):
    name = "opencode"
    billing_options = frozenset({"api_key"})
    default_model = "anthropic/claude-sonnet-4-5"
    command = "opencode"
    # The server writes nothing until the message POST completes; the wrapper
    # owns an earlier deadline so it can abort the OpenCode session cleanly.
    first_byte_seconds = None
    adapter_command = (command, "acp")
    reply_command = (command, "run")

    @classmethod
    def get_acp_session(
        cls,
        account_type: AccountKind,
        model: str,
        prompt: str,
        identity: dict,
        sandbox_home: str,
        conversation_root: str,
    ) -> dict:
        """The adapter reads no _meta: the config rides OPENCODE_CONFIG_CONTENT, and a mode
        is an agent. A database under the conversation root keeps its session apart."""
        config: dict[str, object] = {"$schema": "https://opencode.ai/config.json", "model": model}
        if account_type == AccountKind.OPERATOR:
            files = {f"{conversation_root}/instructions.md": prompt}
            config |= {
                "instructions": [f"{conversation_root}/instructions.md"],
                "permission": "allow",
            }
            mode = "build"
        else:
            denied = ("edit", "bash", "read", "glob", "grep", "list", "webfetch", "websearch")
            permission = json.dumps(dict.fromkeys(denied, "deny"))
            agent = f"---\nmode: primary\npermission: {permission}\n---\n{prompt}\n"
            files = {f"{conversation_root}/.opencode/agents/druks.md": agent}
            mode = "druks"
        return {
            "meta": {},
            "env": {
                "OPENCODE_CONFIG_CONTENT": json.dumps(config, sort_keys=True),
                "OPENCODE_DB": f"{conversation_root}/opencode.db",
            },
            "files": files,
            "mode": mode,
            "model": model,
            # The effort option takes a model variant, and Druks's levels are not variants.
            "options": {"model": "model"},
            "sessionFiles": [f"{conversation_root}/opencode.db*"],
        }

    async def build_invocation(
        self,
        session: AsyncSession,
        *,
        prompt: str,
        schema: dict[str, object],
        run_id: str,
        ssh_username: str,
        include_plugins: bool = True,
        add_dirs: tuple[str, ...] = (),
        skills: tuple[str, ...] = (),
        extra_env: dict[str, str] | None = None,
        mcp_servers: tuple[McpServer, ...] = (),
        identity: dict | None = None,
        timeout: int = Harness.default_timeout,
    ) -> AgentInvocation:
        if not self.sandbox:
            raise exceptions.HarnessError(
                "opencode harness requires sandbox settings — set sandbox.service_url and "
                "related TOML settings.",
            )

        mcp = {}
        for server in mcp_servers:
            headers = dict(server.headers)
            if server.bearer_token_env_var:
                headers["Authorization"] = f"Bearer {{env:{server.bearer_token_env_var}}}"
            for header, env_var in server.env_headers.items():
                headers[header] = f"{{env:{env_var}}}"
            entry: dict[str, object] = {
                "type": "remote",
                "url": server.url,
                "enabled": True,
            }
            if headers:
                entry["headers"] = headers
            if server.bearer_token_env_var or server.env_headers:
                # Druks owns this server's credential; opencode starts no OAuth for it.
                entry["oauth"] = False
            mcp[server.name] = entry

        provider, _, model = self.model.partition("/")
        return AgentInvocation(
            name=self.name,
            args=("sh", "-c", _WRAPPER),
            stdin=prompt.encode("utf-8"),
            credentials=Credentials(),
            env={
                **(extra_env or {}),
                "DRUKS_RUN_DIR": f"{get_runs_root(ssh_username)}/{run_id}",
                "OPENCODE_CONFIG_CONTENT": json.dumps(
                    {"$schema": "https://opencode.ai/config.json", "mcp": mcp},
                    sort_keys=True,
                ),
                "DRUKS_SCHEMA": json.dumps(schema, sort_keys=True),
                "DRUKS_PROVIDER": provider,
                "DRUKS_MODEL": model,
                "DRUKS_WORKSPACE_QUERY": urllib.parse.quote(get_work_root(ssh_username), safe=""),
                "DRUKS_DEADLINE_SECONDS": str(max(1, timeout - _ABORT_MARGIN_SECONDS)),
            },
            extra_artifact_filenames=("opencode.log",),
        )

    def parse(self, result: HarnessRunResult, *, artifact_dir: Path, run_id: str) -> Any:
        if result.returncode == 124:
            raise exceptions.HarnessTimeoutError(
                "opencode hit the agent deadline; the session was aborted."
            )
        try:
            info = json.loads(result.stdout)["info"]
            if failure := info.get("error"):
                data = failure.get("data") or {}
                message = data.get("message")
                detail = f"{failure['name']}: {message}" if message else failure["name"]
                if failure["name"] == "APIError" and data.get("isRetryable"):
                    raise exceptions.HarnessOverloadedError(detail)
                raise _ERROR_TYPES.get(failure["name"], exceptions.HarnessError)(detail)
            structured = info["structured"]
            tokens = info["tokens"]
            metadata = {
                "provider": self.name,
                "model": self.model,
                "input_tokens": tokens["input"],
                "output_tokens": tokens["output"],
                "reasoning_tokens": tokens["reasoning"],
                "cached_input_tokens": tokens["cache"]["read"],
                "cache_creation_tokens": tokens["cache"]["write"],
            }
            cost_usd = float(info["cost"])
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            try:
                self.check_returncode(result)
            except exceptions.HarnessError as process_error:
                raise exceptions.HarnessInvalidOutputError(str(process_error)) from error
            raise exceptions.HarnessInvalidOutputError(
                "opencode wrote no usable response.",
            ) from error

        self.check_returncode(result)
        output_dir = call_dir(artifact_dir, run_id)
        (output_dir / "output.json").write_text(json.dumps(structured, indent=2, sort_keys=True))
        write_cost(output_dir, cost_usd=cost_usd, metadata=metadata)
        return structured
