import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from druks.harnesses.base import Harness
from druks.harnesses.datastructures import SandboxSettings
from druks.harnesses.exceptions import (
    HarnessError,
    HarnessInvalidOutputError,
    HarnessNotConnectedError,
    HarnessOverloadedError,
    HarnessTimeoutError,
)
from druks.harnesses.opencode import OpenCodeHarness
from druks.harnesses.registry import get_harness
from druks.sandbox.datastructures import HarnessRunResult, McpServer
from pydantic import BaseModel

_MODEL = OpenCodeHarness.default_model
_API_KEY = "sk-opencode-secret"  # nosec B105


class _Contract(BaseModel):
    answer: str
    count: int


def _sandbox_config() -> SandboxSettings:
    return SandboxSettings(
        service_url="https://sandbox.test",
        service_token="token",
        service_timeout=30.0,
        image="image",
        claude_config_dir=None,
        codex_config_dir=None,
    )


def _harness() -> OpenCodeHarness:
    return OpenCodeHarness(
        model=_MODEL,
        fast_mode=False,
        effort=None,
        sandbox=_sandbox_config(),
    )


def test_class_facts_and_registration() -> None:
    assert get_harness("opencode") is OpenCodeHarness
    assert OpenCodeHarness.command == "opencode"
    assert OpenCodeHarness.provider is None
    assert OpenCodeHarness.login_kinds == {"api_key"}
    assert OpenCodeHarness.first_byte_seconds is None
    assert OpenCodeHarness.failure_markers == {}
    assert "_model_discovery_request" not in OpenCodeHarness.__dict__
    assert "_usage_request" not in OpenCodeHarness.__dict__


def _response(
    *,
    structured: object = None,
    error: dict[str, object] | None = None,
    cost: float = 0.0,
) -> bytes:
    info: dict[str, object] = {
        "role": "assistant",
        "providerID": "anthropic",
        "modelID": "claude-sonnet-4-5",
        "cost": cost,
        "tokens": {
            "input": 120,
            "output": 30,
            "reasoning": 4,
            "cache": {"read": 20, "write": 10},
        },
    }
    if error:
        info["error"] = error
    else:
        info["structured"] = structured
    return json.dumps({"info": info, "parts": []}).encode()


async def test_build_invocation_uses_server_schema_and_env_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def login(self: Harness, _credential_id: str | None) -> SimpleNamespace:
        return SimpleNamespace(provider="anthropic", payload={"api_key": _API_KEY})

    monkeypatch.setattr(OpenCodeHarness, "login", login)
    server = McpServer(
        name="github",
        url="https://api.example.test/mcp",
        bearer_token_env_var="MCP_GITHUB_TOKEN",
        env_headers={"X-Trace-Key": "MCP_TRACE_KEY"},
    )
    invocation = await _harness().build_invocation(
        prompt="A large prompt stays on stdin.",
        schema={"type": "object", "properties": {"answer": {"type": "string"}}},
        run_id="run-1",
        ssh_username="exedev",
        github_token="github-token",
        extra_env={"MCP_GITHUB_TOKEN": "mcp-secret", "MCP_TRACE_KEY": "trace-secret"},
        mcp_servers=(server,),
        timeout=60,
    )

    assert invocation.name == "opencode"
    assert invocation.args[:2] == ("sh", "-c")
    wrapper = invocation.args[2]
    for text in (
        "opencode serve",
        "--port 0",
        "listening on",
        "--hostname 127.0.0.1 --port 0",
        "/session?directory=",
        "/message",
        "json_schema",
        '"$DRUKS_DEADLINE_SECONDS"',
        "/abort",
    ):
        assert text in wrapper
    assert invocation.stdin == b"A large prompt stays on stdin."
    assert invocation.credentials.home == ()
    assert invocation.credentials.github_token == "github-token"
    env = invocation.env
    assert env is not None
    assert json.loads(env["OPENCODE_AUTH_CONTENT"]) == {
        "anthropic": {"type": "api", "key": _API_KEY}
    }
    assert env["DRUKS_RUN_DIR"].endswith("/run-1")
    assert env["DRUKS_PROVIDER"] == "anthropic"
    assert env["DRUKS_MODEL"] == "claude-sonnet-4-5"
    assert env["DRUKS_DEADLINE_SECONDS"] == "55"
    assert json.loads(env["DRUKS_SCHEMA"]) == {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
    }
    config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert config["mcp"]["github"]["headers"] == {
        "Authorization": "Bearer {env:MCP_GITHUB_TOKEN}",
        "X-Trace-Key": "{env:MCP_TRACE_KEY}",
    }
    assert _API_KEY not in wrapper
    assert "mcp-secret" not in env["OPENCODE_CONFIG_CONTENT"]
    assert "trace-secret" not in env["OPENCODE_CONFIG_CONTENT"]
    syntax = subprocess.run(
        ["sh", "-n", "-c", wrapper],
        check=False,
        capture_output=True,
        text=True,
    )
    assert syntax.returncode == 0, syntax.stderr


def test_auth_json_keys_the_login_provider() -> None:
    login = SimpleNamespace(provider="openai", payload={"api_key": _API_KEY})

    rendered = OpenCodeHarness.auth_json(login)

    assert json.loads(rendered) == {"openai": {"type": "api", "key": _API_KEY}}


def test_parse_returns_contract_and_writes_cost_sidecars(tmp_path: Path) -> None:
    structured = {"answer": "done", "count": 3}

    output = _harness().parse(
        HarnessRunResult(returncode=0, stdout=_response(structured=structured), stderr=b""),
        artifact_dir=tmp_path,
        run_id="run-1",
    )

    assert _Contract.model_validate(output) == _Contract(answer="done", count=3)
    assert json.loads((tmp_path / "run-1" / "output.json").read_text()) == structured
    assert json.loads((tmp_path / "run-1" / "cost.json").read_text()) == {
        "cost_usd": 0.0,
        "metadata": {
            "provider": "opencode",
            "model": _MODEL,
            "input_tokens": 120,
            "output_tokens": 30,
            "reasoning_tokens": 4,
            "cached_input_tokens": 20,
            "cache_creation_tokens": 10,
        },
    }


@pytest.mark.parametrize("stdout", [b"", b"not-json"])
def test_parse_rejects_empty_or_non_json_stdout(tmp_path: Path, stdout: bytes) -> None:
    with pytest.raises(HarnessInvalidOutputError):
        _harness().parse(
            HarnessRunResult(returncode=0, stdout=stdout, stderr=b""),
            artifact_dir=tmp_path,
            run_id="run-1",
        )


def test_parse_maps_the_deadline_exit_to_timeout(tmp_path: Path) -> None:
    with pytest.raises(HarnessTimeoutError, match="deadline"):
        _harness().parse(
            HarnessRunResult(returncode=124, stdout=b"", stderr=b"session aborted\n"),
            artifact_dir=tmp_path,
            run_id="run-1",
        )


def test_parse_includes_stderr_when_failed_process_wrote_no_json(tmp_path: Path) -> None:
    with pytest.raises(HarnessInvalidOutputError, match="server failed"):
        _harness().parse(
            HarnessRunResult(returncode=1, stdout=b"", stderr=b"server failed\n"),
            artifact_dir=tmp_path,
            run_id="run-1",
        )


@pytest.mark.parametrize(
    ("name", "data", "expected"),
    [
        (
            "ProviderAuthError",
            {"providerID": "anthropic", "message": "bad key"},
            HarnessNotConnectedError,
        ),
        (
            "StructuredOutputError",
            {"message": "schema mismatch", "retries": 0},
            HarnessInvalidOutputError,
        ),
        ("ContextOverflowError", {"message": "too much context"}, HarnessError),
        ("ContentFilterError", {"message": "blocked"}, HarnessError),
        ("MessageOutputLengthError", {}, HarnessError),
        ("MessageAborted", {"message": "deadline"}, HarnessTimeoutError),
        (
            "APIError",
            {"message": "busy", "isRetryable": True},
            HarnessOverloadedError,
        ),
        (
            "APIError",
            {"message": "bad request", "isRetryable": False},
            HarnessError,
        ),
    ],
)
def test_parse_maps_error_union(
    tmp_path: Path,
    name: str,
    data: dict[str, object],
    expected: type[HarnessError],
) -> None:
    result = HarnessRunResult(
        returncode=1,
        stdout=_response(error={"name": name, "data": data}),
        stderr=b"wrapper failed",
    )

    with pytest.raises(expected) as error:
        _harness().parse(result, artifact_dir=tmp_path, run_id="run-1")

    assert type(error.value) is expected
    assert name in str(error.value)
