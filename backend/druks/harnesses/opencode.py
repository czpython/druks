import json
import logging
import urllib.parse
from pathlib import Path
from typing import Any

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
from .models import ProviderSubscription

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

    @classmethod
    def auth_json(cls, provider: str, key: str) -> str:
        """The auth store opencode reads from OPENCODE_AUTH_CONTENT."""
        return json.dumps({provider: {"type": "api", "key": key}})

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
            mcp[server.name] = entry

        provider, _, model = self.model.partition("/")
        return AgentInvocation(
            name=self.name,
            args=("sh", "-c", _WRAPPER),
            stdin=prompt.encode("utf-8"),
            credentials=Credentials(github_token=github_token),
            env={
                **(extra_env or {}),
                "OPENCODE_AUTH_CONTENT": self.auth_json(provider, key),
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
