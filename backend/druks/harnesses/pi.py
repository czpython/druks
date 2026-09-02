import json
import re
import shlex
from pathlib import Path
from typing import Any

from druks.sandbox.datastructures import (
    AgentInvocation,
    Credentials,
    HarnessRunResult,
    HomeFile,
    McpServer,
)
from druks.sandbox.layout import get_runs_root

from . import exceptions
from .artifacts import write_cost
from .base import Harness
from .models import ProviderLogin
from .subprocess import read_result_json

_DRUKS_OUTPUT_TEMPLATE = Path(__file__).parent / "druks-output.ts"

# deploy/sandbox/Dockerfile installs pi-mcp-adapter globally and asserts this path.
_PI_MCP_ADAPTER_PATH = "/usr/lib/node_modules/pi-mcp-adapter/index.ts"
_PROVIDER_ERROR_STATUS = re.compile(r" API error \((\d{3})\):")
_STATUS_ERRORS = {
    401: exceptions.HarnessAuthError,
    403: exceptions.HarnessAuthError,
    429: exceptions.HarnessRateLimitError,
}


class PiHarness(Harness):
    name = "pi"
    command = "pi"
    login_kinds = frozenset({"api_key"})
    models = ("openai/gpt-5.5",)
    default_model = "openai/gpt-5.5"

    @classmethod
    def auth_file(cls, login: ProviderLogin) -> HomeFile:
        auth = {login.provider: {"type": "api_key", "key": login.payload["api_key"]}}
        return HomeFile(".pi/agent/auth.json", json.dumps(auth))

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

        provider, _, model = self.model.partition("/")
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

        auth_file = self.auth_file(await self.login(connection_id))
        return AgentInvocation(
            name=self.name,
            args=("sh", "-c", wrapper),
            stdin=prompt.encode("utf-8"),
            credentials=Credentials(home=(auth_file,), github_token=github_token),
            env={
                **(extra_env or {}),
                "DRUKS_SCHEMA_PATH": in_vm_schema,
                "DRUKS_RESULT_PATH": in_vm_output,
            },
            extra_artifact_filenames=("output.json",),
        )

    def parse(self, result: HarnessRunResult, *, artifact_dir: Path, run_id: str) -> Any:
        self.check_returncode(result)
        try:
            events = [json.loads(line) for line in result.stdout.splitlines()]
            # pi repeats an assistant message on turn_end and agent_end;
            # message_end is the one place each appears once.
            messages = [
                event["message"]
                for event in events
                if event["type"] == "message_end" and event["message"]["role"] == "assistant"
            ]
            for message in messages:
                # A runtime provider failure exits zero, so the HTTP status in
                # ``errorMessage`` is what classifies it.
                if message["stopReason"] == "error":
                    detail = message["errorMessage"]
                    status = _PROVIDER_ERROR_STATUS.search(detail)
                    code = int(status.group(1)) if status else 0
                    if code >= 500:
                        raise exceptions.HarnessOverloadedError(detail)
                    raise _STATUS_ERRORS.get(code, exceptions.HarnessError)(detail)
            usage = [message["usage"] for message in messages]
            cost_usd = sum(entry["cost"]["total"] for entry in usage)
            tokens = {
                "input_tokens": sum(entry["input"] for entry in usage),
                "output_tokens": sum(entry["output"] for entry in usage),
                "cached_input_tokens": sum(entry["cacheRead"] for entry in usage),
                "cache_creation_tokens": sum(entry["cacheWrite"] for entry in usage),
            }
        except (ValueError, KeyError, TypeError) as error:
            raise exceptions.HarnessInvalidOutputError("pi wrote no usable stream.") from error

        call_dir = artifact_dir / run_id
        output_path = call_dir / "output.json"
        payload = read_result_json(output_path, name=self.name)
        output_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        write_cost(
            call_dir,
            cost_usd=cost_usd,
            metadata={"provider": self.name, "model": self.model, **tokens},
        )
        return payload
