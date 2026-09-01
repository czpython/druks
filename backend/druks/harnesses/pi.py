import json
import re
import shlex
from pathlib import Path
from typing import Any

from druks.sandbox.datastructures import (
    AgentInvocation,
    Credentials,
    HarnessRunResult,
    McpServer,
)
from druks.sandbox.layout import get_runs_root

from . import exceptions
from .artifacts import write_cost
from .base import Harness
from .subprocess import read_result_json

_DRUKS_OUTPUT_TEMPLATE = Path(__file__).parent / "druks-output.ts"

# deploy/sandbox/Dockerfile installs pi-mcp-adapter globally and asserts this path.
_PI_MCP_ADAPTER_PATH = "/usr/lib/node_modules/pi-mcp-adapter/index.ts"
_PROVIDER_ERROR_STATUS = re.compile(r" API error \((\d{3})\):")


def _assistant_messages(stdout: bytes) -> list[dict]:
    messages = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(event, dict) and event.get("type") == "message_end":
            message = event.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                messages.append(message)
    return messages


def _provider_error(message: dict) -> exceptions.HarnessError:
    """pi exits zero on a runtime provider failure, so the HTTP status carried in
    ``errorMessage`` is what classifies it."""
    error_message = message.get("errorMessage")
    if not isinstance(error_message, str):
        return exceptions.HarnessError("pi reported an error with no message")
    status_match = _PROVIDER_ERROR_STATUS.search(error_message)
    if status_match:
        status = int(status_match.group(1))
        if status in {401, 403}:
            return exceptions.HarnessAuthError(error_message)
        if status == 429:
            return exceptions.HarnessRateLimitError(error_message)
        if status >= 500:
            return exceptions.HarnessOverloadedError(error_message)
    return exceptions.HarnessError(error_message)


def _spend(messages: list[dict]) -> tuple[float, dict[str, int]]:
    """The run's summed cost and token counts, keyed as the cost sidecar reads them."""
    cost_usd = 0.0
    tokens = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "cache_creation_tokens": 0,
    }
    for message in messages:
        usage = message.get("usage")
        if isinstance(usage, dict):
            tokens["input_tokens"] += usage.get("input", 0)
            tokens["output_tokens"] += usage.get("output", 0)
            tokens["cached_input_tokens"] += usage.get("cacheRead", 0)
            tokens["cache_creation_tokens"] += usage.get("cacheWrite", 0)
            cost = usage.get("cost")
            if isinstance(cost, dict):
                cost_usd += cost.get("total", 0)
    return cost_usd, tokens


class PiHarness(Harness):
    name = "pi"
    provider = "pi"
    command = "pi"
    credentials_path = ".pi/agent/auth.json"
    login_kinds = frozenset({"api_key"})
    models = ("openai/gpt-5.5",)
    default_model = "openai/gpt-5.5"

    @classmethod
    async def render_credentials_file(
        cls,
        connection_id: str | None = None,
        *,
        model: str | None = None,
    ) -> str:
        payload = json.loads(await super().render_credentials_file(connection_id))
        provider, _ = cls._model_parts(model or cls.default_model)
        return json.dumps({provider: {"type": "api_key", "key": payload["apiKey"]}})

    async def build_invocation(
        self,
        *,
        prompt: str,
        schema: dict[str, object],
        run_id: str,
        ssh_username: str,
        github_token: str | None = None,
        # Accepted for signature parity and dropped: pi has no plugin layer, it
        # runs with full filesystem access, and --no-skills is what keeps the
        # run hermetic.
        include_plugins: bool = True,
        add_dirs: tuple[str, ...] = (),
        skills: tuple[str, ...] = (),
        extra_env: dict[str, str] | None = None,
        mcp_servers: tuple[McpServer, ...] = (),
        connection_id: str | None = None,
        timeout: int = Harness.default_timeout,
    ) -> AgentInvocation:
        if not self.sandbox:
            raise exceptions.HarnessError(
                f"{self.name} harness requires sandbox settings — set "
                "sandbox.service_url and related TOML settings.",
            )

        provider, model = self._model_parts(self.model)
        in_vm_run_dir = f"{get_runs_root(ssh_username)}/{run_id}"
        in_vm_schema = f"{in_vm_run_dir}/schema.json"
        in_vm_extension = f"{in_vm_run_dir}/druks-output.ts"
        in_vm_output = f"{in_vm_run_dir}/output.json"
        in_vm_mcp_config = f"{in_vm_run_dir}/.mcp.json"

        schema_body = json.dumps(schema, indent=2, sort_keys=True)
        extension_body = _DRUKS_OUTPUT_TEMPLATE.read_text()
        mcp = {}
        for server in mcp_servers:
            headers = dict(server.headers)
            if server.bearer_token_env_var:
                headers["Authorization"] = f"Bearer ${{{server.bearer_token_env_var}}}"
            for header, env_var in server.env_headers.items():
                headers[header] = f"${{{env_var}}}"
            # Druks owns MCP authentication, so the adapter must not start headless OAuth.
            entry: dict[str, object] = {"url": server.url, "auth": False}
            if headers:
                entry["headers"] = headers
            mcp[server.name] = entry

        command = [
            self.command,
            "-p",
            "--mode",
            "json",
            "--no-session",
            "--no-extensions",
            "--no-skills",
            "--offline",
            "--provider",
            provider,
            "--model",
            model,
            "-e",
            in_vm_extension,
        ]
        if self.effort:
            command.extend(("--thinking", self.effort))
        if mcp:
            command.extend(("-e", _PI_MCP_ADAPTER_PATH, "--mcp-config", in_vm_mcp_config))

        writes = [
            f"mkdir -p {shlex.quote(in_vm_run_dir)}",
            f"printf %s {shlex.quote(schema_body)} > {shlex.quote(in_vm_schema)}",
            f"printf %s {shlex.quote(extension_body)} > {shlex.quote(in_vm_extension)}",
        ]
        if mcp:
            mcp_body = json.dumps({"mcpServers": mcp}, indent=2, sort_keys=True)
            writes.append(f"printf %s {shlex.quote(mcp_body)} > {shlex.quote(in_vm_mcp_config)}")
        command_line = " ".join(shlex.quote(argument) for argument in command)
        wrapper = " && ".join((*writes, command_line))

        rendered_credentials = await self.render_credentials_file(
            connection_id,
            model=self.model,
        )
        return AgentInvocation(
            name=self.name,
            args=("sh", "-c", wrapper),
            stdin=prompt.encode("utf-8"),
            credentials=Credentials(
                files=((self.credentials_path, rendered_credentials),),
                github_token=github_token,
            ),
            env={
                **(extra_env or {}),
                "DRUKS_SCHEMA_PATH": in_vm_schema,
                "DRUKS_RESULT_PATH": in_vm_output,
            },
            extra_artifact_filenames=("output.json",),
        )

    def parse(self, result: HarnessRunResult, *, artifact_dir: Path, run_id: str) -> Any:
        self.check_returncode(result)

        messages = _assistant_messages(result.stdout)
        for message in messages:
            if message.get("stopReason") == "error":
                raise _provider_error(message)

        call_dir = artifact_dir / run_id
        output_path = call_dir / "output.json"
        payload = read_result_json(output_path, name=self.name)
        output_path.write_text(json.dumps(payload, indent=2, sort_keys=True))

        cost_usd, tokens = _spend(messages)
        write_cost(
            call_dir,
            cost_usd=cost_usd,
            metadata={"provider": self.provider, "model": self.model, **tokens},
        )
        return payload

    @staticmethod
    def _model_parts(model_id: str | None) -> tuple[str, str]:
        provider, separator, model = (model_id or "").partition("/")
        if not separator or not provider or not model:
            raise exceptions.HarnessError(
                f"pi model must use provider/model form, got {model_id!r}.",
            )
        return provider, model
