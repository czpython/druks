import json
import logging
import urllib.parse
from pathlib import Path
from typing import Any

import httpx

from druks.sandbox.datastructures import (
    AgentInvocation,
    Credentials,
    HarnessRunResult,
    McpServer,
)
from druks.sandbox.layout import get_runs_root, get_work_root

from . import exceptions
from .artifacts import call_dir, write_cost
from .base import Harness, _error_tag
from .datastructures import ParsedModels
from .models import HarnessConnection

logger = logging.getLogger(__name__)

_ABORT_MARGIN_SECONDS = 5
_CATALOG_URL = "https://models.dev/api.json"
_DISCOVERY_TIMEOUT_SECONDS = 10.0
_WRAPPER = (Path(__file__).parent / "opencode_wrapper.sh").read_text()
_ERROR_TYPES = {
    "ProviderAuthError": exceptions.HarnessNotConnectedError,
    "StructuredOutputError": exceptions.HarnessInvalidOutputError,
    "MessageAborted": exceptions.HarnessTimeoutError,
}


class OpenCodeHarness(Harness):
    name = "opencode"
    provider = "opencode"
    models = ("anthropic/claude-sonnet-4-5",)
    default_model = "anthropic/claude-sonnet-4-5"
    command = "opencode"
    login_kinds = frozenset({"api_key"})
    # The server writes nothing until the message POST completes; the wrapper
    # owns an earlier deadline so it can abort the OpenCode session cleanly.
    first_byte_seconds = None

    @classmethod
    async def render_credentials_file(
        cls,
        connection_id: str | None = None,
        *,
        model: str | None = None,
    ) -> str:
        payload = json.loads(await super().render_credentials_file(connection_id))
        provider, _, _ = (model or cls.default_model).partition("/")
        return json.dumps({provider: {"type": "api", "key": payload["api_key"]}})

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
        connection_id: str | None = None,
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
                "OPENCODE_AUTH_CONTENT": await self.render_credentials_file(
                    connection_id, model=self.model
                ),
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
                "provider": self.provider,
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

    @classmethod
    async def fetch_models(cls, _connection: HarnessConnection) -> ParsedModels:
        # opencode's catalog IS models.dev (same team; its docs say so), so the
        # picker reads the upstream directly — no opencode binary on the druks
        # host. Advisory either way: the CLI's own listing gates on key
        # presence, not validity.
        try:
            async with httpx.AsyncClient(timeout=_DISCOVERY_TIMEOUT_SECONDS) as client:
                response = await client.get(_CATALOG_URL)
        except httpx.TimeoutException:
            return ParsedModels(ok=False, error="timeout")
        except httpx.HTTPError as error:
            logger.warning("models request failed for %s: %s", cls.name, error, exc_info=True)
            return ParsedModels(ok=False, error="network")
        if response.status_code != 200:
            logger.warning(
                "models endpoint %s for %s: %s",
                response.status_code,
                cls.name,
                response.text[:300],
            )
            return ParsedModels(ok=False, error=_error_tag(response.status_code))

        raw = response.text
        # The provider is the model namespace — the same rule the auth store
        # rendering uses.
        provider, _, _ = cls.default_model.partition("/")
        try:
            models = tuple(
                {"id": f"{provider}/{model['id']}", "label": model["name"]}
                for model in json.loads(raw)[provider]["models"].values()
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            return ParsedModels(ok=False, error="unexpected_payload", raw=raw)
        if not models:
            return ParsedModels(ok=False, error="empty_list", raw=raw)
        return ParsedModels(ok=True, models=models, raw=raw)
