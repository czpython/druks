import json
import shlex
import subprocess
from pathlib import Path

import pytest
from druks.harnesses.base import Harness
from druks.harnesses.datastructures import SandboxSettings
from druks.harnesses.exceptions import (
    HarnessAuthError,
    HarnessError,
    HarnessInvalidOutputError,
    HarnessOverloadedError,
    HarnessRateLimitError,
)
from druks.harnesses.pi import PiHarness
from druks.harnesses.registry import get_harness
from druks.sandbox.datastructures import HarnessRunResult, McpServer
from pydantic import BaseModel

_MODEL = PiHarness.default_model
_API_KEY = "sk-pi-secret"  # nosec B105
_ADAPTER_PATH = "/usr/lib/node_modules/pi-mcp-adapter/index.ts"


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


def _harness(*, effort: str | None = "high", sandbox: bool = True) -> PiHarness:
    return PiHarness(
        model=_MODEL,
        fast_mode=False,
        effort=effort,
        sandbox=_sandbox_config() if sandbox else None,
    )


async def _credentials(cls: type[Harness]) -> dict[str, str]:
    return {"apiKey": _API_KEY}


def test_class_facts_and_registration() -> None:
    assert get_harness("pi") is PiHarness
    assert PiHarness.name == "pi"
    assert PiHarness.provider == "pi"
    assert PiHarness.command == "pi"
    assert PiHarness.credentials_path == ".pi/agent/auth.json"
    assert PiHarness.login_kinds == frozenset({"api_key"})
    assert PiHarness.models == ("openai/gpt-5.5",)
    assert PiHarness.default_model == "openai/gpt-5.5"
    assert PiHarness.first_byte_seconds == Harness.first_byte_seconds == 90
    assert PiHarness.failure_markers is Harness.failure_markers
    assert "_model_discovery_request" not in PiHarness.__dict__
    assert "_usage_request" not in PiHarness.__dict__


async def test_build_invocation_writes_files_and_uses_pi_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(PiHarness, "get_credentials", classmethod(_credentials))
    server = McpServer(
        name="github",
        url="https://api.example.test/mcp",
        bearer_token_env_var="MCP_GITHUB_TOKEN",
        headers={"X-Visible": "public"},
        env_headers={"X-Trace-Key": "MCP_TRACE_KEY"},
    )
    prompt = "A large prompt stays on stdin."
    invocation = await _harness().build_invocation(
        prompt=prompt,
        schema={"type": "object", "properties": {"answer": {"type": "string"}}},
        run_id="run-1",
        ssh_username="exedev",
        github_token="github-token",
        extra_env={
            "MCP_GITHUB_TOKEN": "mcp-secret",
            "MCP_TRACE_KEY": "trace-secret",
        },
        mcp_servers=(server,),
    )

    assert invocation.name == "pi"
    assert invocation.args[:2] == ("sh", "-c")
    wrapper = invocation.args[2]
    wrapper_parts = shlex.split(wrapper)
    pi_index = wrapper_parts.index("pi")
    assert wrapper_parts[pi_index:] == [
        "pi",
        "-p",
        "--mode",
        "json",
        "--no-session",
        "--no-extensions",
        "--no-skills",
        "--offline",
        "--provider",
        "openai",
        "--model",
        "gpt-5.5",
        "-e",
        "/home/exedev/work/runs/run-1/druks-output.ts",
        "--thinking",
        "high",
        "-e",
        _ADAPTER_PATH,
        "--mcp-config",
        "/home/exedev/work/runs/run-1/.mcp.json",
    ]
    for filename in ("schema.json", "druks-output.ts", ".mcp.json"):
        assert f"/home/exedev/work/runs/run-1/{filename}" in wrapper_parts
    schema_path_index = wrapper_parts.index("/home/exedev/work/runs/run-1/schema.json")
    assert json.loads(wrapper_parts[schema_path_index - 2]) == {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
    }
    extension_path_index = wrapper_parts.index("/home/exedev/work/runs/run-1/druks-output.ts")
    assert "pi.registerTool" in wrapper_parts[extension_path_index - 2]
    assert invocation.stdin == prompt.encode()
    assert prompt not in wrapper
    assert all(prompt not in argument for argument in invocation.args)
    for secret in (_API_KEY, "mcp-secret", "trace-secret"):
        assert secret not in wrapper
        assert all(secret not in argument for argument in invocation.args)
    assert invocation.cwd is None
    assert invocation.env == {
        "MCP_GITHUB_TOKEN": "mcp-secret",
        "MCP_TRACE_KEY": "trace-secret",
        "DRUKS_SCHEMA_PATH": "/home/exedev/work/runs/run-1/schema.json",
        "DRUKS_RESULT_PATH": "/home/exedev/work/runs/run-1/output.json",
    }
    assert invocation.credentials.github_token == "github-token"
    assert invocation.credentials.files[0][0] == ".pi/agent/auth.json"
    assert json.loads(invocation.credentials.files[0][1]) == {
        "openai": {"type": "api_key", "key": _API_KEY}
    }
    assert invocation.extra_artifact_filenames == ("output.json",)
    syntax = subprocess.run(
        ["sh", "-n", "-c", wrapper],
        check=False,
        capture_output=True,
        text=True,
    )
    assert syntax.returncode == 0, syntax.stderr


async def test_build_invocation_omits_mcp_adapter_without_servers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(PiHarness, "get_credentials", classmethod(_credentials))

    invocation = await _harness(effort=None).build_invocation(
        prompt="Prompt",
        schema={"type": "object"},
        run_id="run-1",
        ssh_username="exedev",
    )

    wrapper = invocation.args[2]
    assert _ADAPTER_PATH not in wrapper
    assert "--mcp-config" not in wrapper
    assert ".mcp.json" not in wrapper
    assert "--thinking" not in wrapper


async def test_build_invocation_writes_standard_mcp_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(PiHarness, "get_credentials", classmethod(_credentials))
    server = McpServer(
        name="github",
        url="https://api.example.test/mcp",
        bearer_token_env_var="MCP_GITHUB_TOKEN",
        headers={"X-Visible": "public"},
        env_headers={"X-Trace-Key": "MCP_TRACE_KEY"},
    )
    public_server = McpServer(
        name="public",
        url="https://public.example.test/mcp",
    )

    invocation = await _harness().build_invocation(
        prompt="Prompt",
        schema={"type": "object"},
        run_id="run-1",
        ssh_username="exedev",
        extra_env={
            "MCP_GITHUB_TOKEN": "mcp-secret",
            "MCP_TRACE_KEY": "trace-secret",
        },
        mcp_servers=(server, public_server),
    )

    wrapper_parts = shlex.split(invocation.args[2])
    mcp_path_index = wrapper_parts.index("/home/exedev/work/runs/run-1/.mcp.json")
    mcp_body = wrapper_parts[mcp_path_index - 2]
    assert json.loads(mcp_body) == {
        "mcpServers": {
            "github": {
                "url": "https://api.example.test/mcp",
                "auth": False,
                "headers": {
                    "Authorization": "Bearer ${MCP_GITHUB_TOKEN}",
                    "X-Trace-Key": "${MCP_TRACE_KEY}",
                    "X-Visible": "public",
                },
            },
            "public": {
                "url": "https://public.example.test/mcp",
                "auth": False,
            },
        }
    }
    assert "mcp-secret" not in mcp_body
    assert "trace-secret" not in mcp_body


async def test_build_invocation_requires_sandbox_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(PiHarness, "get_credentials", classmethod(_credentials))

    with pytest.raises(HarnessError, match="pi harness requires sandbox settings"):
        await _harness(sandbox=False).build_invocation(
            prompt="Prompt",
            schema={"type": "object"},
            run_id="run-1",
            ssh_username="exedev",
        )


def test_parse_returns_contract_and_writes_summed_cost(tmp_path: Path) -> None:
    structured = {"answer": "done", "count": 3}
    output_dir = tmp_path / "run-1"
    output_dir.mkdir()
    (output_dir / "output.json").write_text(json.dumps(structured))
    first_message = {
        "role": "assistant",
        "usage": {
            "input": 100,
            "output": 20,
            "cacheRead": 5,
            "cacheWrite": 2,
            "cost": {"total": 0.125},
        },
    }
    second_message = {
        "role": "assistant",
        "usage": {
            "input": 40,
            "output": 10,
            "cacheRead": 4,
            "cacheWrite": 1,
            "cost": {"total": 0.375},
        },
    }
    stdout = b"\n".join(
        (
            b"not json",
            json.dumps({"type": "message_end", "message": first_message}).encode(),
            json.dumps({"type": "message_end", "message": second_message}).encode(),
            json.dumps({"type": "turn_end", "message": second_message}).encode(),
            json.dumps({"type": "agent_end", "messages": [first_message, second_message]}).encode(),
        )
    )

    output = _harness().parse(
        HarnessRunResult(returncode=0, stdout=stdout, stderr=b""),
        artifact_dir=tmp_path,
        run_id="run-1",
    )

    assert _Contract.model_validate(output) == _Contract(answer="done", count=3)
    assert (output_dir / "output.json").read_text() == json.dumps(
        structured,
        indent=2,
        sort_keys=True,
    )
    assert json.loads((output_dir / "cost.json").read_text()) == {
        "cost_usd": 0.5,
        "metadata": {
            "provider": "pi",
            "model": _MODEL,
            "input_tokens": 140,
            "output_tokens": 30,
            "cached_input_tokens": 9,
            "cache_creation_tokens": 3,
        },
    }


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, HarnessAuthError),
        (403, HarnessAuthError),
        (429, HarnessRateLimitError),
        (500, HarnessOverloadedError),
        (503, HarnessOverloadedError),
        (418, HarnessError),
        (None, HarnessError),
    ],
)
def test_parse_maps_provider_error_status(
    tmp_path: Path,
    status: int | None,
    expected: type[HarnessError],
) -> None:
    error_message = (
        f"OpenAI API error ({status}): provider detail"
        if status
        else "OpenAI provider error without a status"
    )
    stdout = json.dumps(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "error",
                "errorMessage": error_message,
            },
        }
    ).encode()

    with pytest.raises(expected) as error:
        _harness().parse(
            HarnessRunResult(returncode=0, stdout=stdout, stderr=b""),
            artifact_dir=tmp_path,
            run_id="run-1",
        )

    assert type(error.value) is expected
    assert str(error.value) == error_message


def test_parse_checks_returncode_before_provider_error(tmp_path: Path) -> None:
    stdout = json.dumps(
        {
            "type": "message_end",
            "message": {
                "stopReason": "error",
                "errorMessage": "OpenAI API error (401): bad key",
            },
        }
    ).encode()

    with pytest.raises(HarnessError, match="pi exited with 1.*extension failed") as error:
        _harness().parse(
            HarnessRunResult(returncode=1, stdout=stdout, stderr=b"extension failed\n"),
            artifact_dir=tmp_path,
            run_id="run-1",
        )

    assert type(error.value) is HarnessError


def test_parse_rejects_missing_result_without_error_event(tmp_path: Path) -> None:
    stdout = b'not json\n{"type":"agent_settled"}'

    with pytest.raises(HarnessInvalidOutputError, match="pi did not write result JSON"):
        _harness().parse(
            HarnessRunResult(returncode=0, stdout=stdout, stderr=b""),
            artifact_dir=tmp_path,
            run_id="run-1",
        )


async def test_render_credentials_file_uses_model_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(PiHarness, "get_credentials", classmethod(_credentials))

    rendered = await _harness().render_credentials_file(model="openai/gpt-5.5")

    assert json.loads(rendered) == {"openai": {"type": "api_key", "key": _API_KEY}}


def test_model_parts_requires_provider_qualified_model() -> None:
    with pytest.raises(HarnessError, match="pi model must use provider/model form"):
        PiHarness._model_parts("gpt-5.5")
