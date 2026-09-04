import base64
import json
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from druks.accounts.models import Account
from druks.harnesses.datastructures import SandboxSettings
from druks.harnesses.exceptions import (
    HarnessAuthError,
    HarnessError,
    HarnessInvalidOutputError,
    HarnessOverloadedError,
    HarnessRateLimitError,
)
from druks.harnesses.models import ProviderLogin
from druks.harnesses.pi import PiHarness
from druks.harnesses.registry import get_harness
from druks.sandbox.datastructures import HarnessRunResult, HomeFile, McpServer
from druks.testing import configure_app_for_test, make_settings
from fastapi.testclient import TestClient
from pydantic import BaseModel

_API_KEY = "sk-pi-secret"  # nosec B105
_RUN_DIR = "/home/exedev/work/runs/run-1"


class _Contract(BaseModel):
    answer: str
    count: int


def _harness(*, effort: str | None = "high") -> PiHarness:
    return PiHarness(
        model=PiHarness.default_model,
        fast_mode=False,
        effort=effort,
        sandbox=SandboxSettings(
            service_url="https://sandbox.test",
            service_token="token",
            service_timeout=30.0,
            image="image",
            claude_config_dir=None,
            codex_config_dir=None,
        ),
    )


_LOGIN = SimpleNamespace(provider="openai", kind="api_key", payload={"api_key": _API_KEY})


@pytest.fixture
def client(tmp_path: Path):
    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as c:
        yield c


def _parse(
    stdout: bytes, artifact_dir: Path, *, returncode: int = 0, stderr: bytes = b""
) -> object:
    return _harness().parse(
        HarnessRunResult(returncode=returncode, stdout=stdout, stderr=stderr),
        artifact_dir=artifact_dir,
        run_id="run-1",
    )


def _message_end(role: str, **message: object) -> bytes:
    return json.dumps({"type": "message_end", "message": {"role": role, **message}}).encode()


def test_class_facts_and_registration() -> None:
    assert get_harness("pi") is PiHarness
    assert PiHarness.command == "pi"
    assert PiHarness.provider is None
    assert PiHarness.login_kinds == {"api_key"}


def test_auth_file_renders_a_key_under_the_vendor() -> None:
    rendered = PiHarness.auth_file(
        SimpleNamespace(provider="anthropic", kind="api_key", payload={"api_key": _API_KEY})
    )
    assert rendered.path == ".pi/agent/auth.json"
    assert json.loads(rendered.content) == {"anthropic": {"type": "api_key", "key": _API_KEY}}


def test_auth_file_renders_an_openai_subscription_as_pis_openai_codex() -> None:
    # pi keeps the ChatGPT backend as its own provider name; the login's kind
    # says which one it is, not the druks provider id.
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    claims = base64.urlsafe_b64encode(b'{"exp": 1800000000}').rstrip(b"=").decode()
    login = SimpleNamespace(
        provider="openai",
        kind="oauth",
        payload={
            "tokens": {
                "access_token": f"{header}.{claims}.sig",
                "refresh_token": "R0",
                "account_id": "acc-1",
            }
        },
    )
    assert json.loads(PiHarness.auth_file(login).content) == {
        "openai-codex": {
            "type": "oauth",
            "access": f"{header}.{claims}.sig",
            "refresh": "R0",
            "expires": 1800000000000,
            "accountId": "acc-1",
        }
    }
    assert PiHarness.provider_name(login) == "openai-codex"


async def test_build_invocation_writes_the_run_files_and_pi_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github = McpServer(
        name="github",
        url="https://api.example.test/mcp",
        bearer_token_env_var="MCP_GITHUB_TOKEN",
        headers={"X-Visible": "public"},
        env_headers={"X-Trace-Key": "MCP_TRACE_KEY"},
    )
    public = McpServer(name="public", url="https://public.example.test/mcp")

    invocation = await _harness().build_invocation(
        login=_LOGIN,
        prompt="A large prompt stays on stdin.",
        schema={"type": "object"},
        run_id="run-1",
        ssh_username="exedev",
        github_token="github-token",
        extra_env={"MCP_GITHUB_TOKEN": "mcp-secret", "MCP_TRACE_KEY": "trace-secret"},
        mcp_servers=(github, public),
    )

    assert invocation.args[:2] == ("sh", "-c")
    wrapper = invocation.args[2]
    parts = shlex.split(wrapper)
    assert parts[parts.index("pi") :] == [
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
        f"{_RUN_DIR}/druks-output.ts",
        "--thinking",
        "high",
        "-e",
        "/usr/lib/node_modules/pi-mcp-adapter/index.ts",
        "--mcp-config",
        f"{_RUN_DIR}/.mcp.json",
    ]
    written = {parts[i + 1]: parts[i - 1] for i, part in enumerate(parts) if part == ">"}
    assert json.loads(written[f"{_RUN_DIR}/schema.json"]) == {"type": "object"}
    assert "pi.registerTool" in written[f"{_RUN_DIR}/druks-output.ts"]
    assert json.loads(written[f"{_RUN_DIR}/.mcp.json"]) == {
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
            "public": {"url": "https://public.example.test/mcp", "auth": False},
        }
    }
    assert invocation.stdin == b"A large prompt stays on stdin."
    assert invocation.env == {
        "MCP_GITHUB_TOKEN": "mcp-secret",
        "MCP_TRACE_KEY": "trace-secret",
        "DRUKS_SCHEMA_PATH": f"{_RUN_DIR}/schema.json",
        "DRUKS_RESULT_PATH": f"{_RUN_DIR}/output.json",
    }
    assert invocation.credentials.github_token == "github-token"
    assert invocation.credentials.home == (
        HomeFile(
            ".pi/agent/auth.json", json.dumps({"openai": {"type": "api_key", "key": _API_KEY}})
        ),
    )
    assert invocation.extra_artifact_filenames == ("output.json",)
    for secret in (_API_KEY, "mcp-secret", "trace-secret"):
        assert secret not in wrapper
    syntax = subprocess.run(
        ["sh", "-n", "-c", wrapper], check=False, capture_output=True, text=True
    )
    assert syntax.returncode == 0, syntax.stderr


async def test_build_invocation_without_servers_or_effort_is_bare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocation = await _harness(effort=None).build_invocation(
        login=_LOGIN,
        prompt="Prompt",
        schema={"type": "object"},
        run_id="run-1",
        ssh_username="exedev",
    )

    wrapper = invocation.args[2]
    assert "--thinking" not in wrapper
    assert "--mcp-config" not in wrapper
    assert "pi-mcp-adapter" not in wrapper
    assert ".mcp.json" not in wrapper


def test_auth_file_keys_the_login_provider() -> None:
    login = SimpleNamespace(provider="anthropic", kind="api_key", payload={"api_key": _API_KEY})

    auth = PiHarness.auth_file(login)

    assert auth.path == ".pi/agent/auth.json"
    assert json.loads(auth.content) == {"anthropic": {"type": "api_key", "key": _API_KEY}}


def test_parse_returns_the_contract_and_sums_spend(tmp_path: Path) -> None:
    (tmp_path / "run-1").mkdir()
    (tmp_path / "run-1" / "output.json").write_text('{"answer": "done", "count": 3}')
    first = {
        "stopReason": "stop",
        "usage": {
            "input": 100,
            "output": 20,
            "cacheRead": 5,
            "cacheWrite": 2,
            "cost": {"total": 0.125},
        },
    }
    second = {
        "stopReason": "toolUse",
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
            b'{"type":"session","version":3}',
            _message_end("user", content=[]),
            _message_end("assistant", **first),
            _message_end("assistant", **second),
            json.dumps({"type": "turn_end", "message": {"role": "assistant", **second}}).encode(),
            json.dumps({"type": "agent_end", "messages": [first, second]}).encode(),
        )
    )

    output = _parse(stdout, tmp_path)

    assert _Contract.model_validate(output) == _Contract(answer="done", count=3)
    assert json.loads((tmp_path / "run-1" / "cost.json").read_text()) == {
        "cost_usd": 0.5,
        "metadata": {
            "provider": "pi",
            "model": "openai/gpt-5.5",
            "input_tokens": 140,
            "output_tokens": 30,
            "cached_input_tokens": 9,
            "cache_creation_tokens": 3,
        },
    }


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ("OpenAI API error (401): bad key", HarnessAuthError),
        ("OpenAI API error (429): slow down", HarnessRateLimitError),
        ("OpenAI API error (503): busy", HarnessOverloadedError),
        ("stream ended without a status", HarnessError),
    ],
)
def test_parse_classifies_a_provider_error_by_status(
    tmp_path: Path, detail: str, expected: type[HarnessError]
) -> None:
    stdout = _message_end("assistant", stopReason="error", errorMessage=detail)

    with pytest.raises(expected) as error:
        _parse(stdout, tmp_path)

    assert type(error.value) is expected
    assert str(error.value) == detail


def test_parse_reports_a_nonzero_exit_before_reading_the_stream(tmp_path: Path) -> None:
    stdout = _message_end("assistant", stopReason="error", errorMessage="OpenAI API error (401): x")

    with pytest.raises(HarnessError, match="pi exited with 1.*extension failed") as error:
        _parse(stdout, tmp_path, returncode=1, stderr=b"extension failed\n")

    assert type(error.value) is HarnessError


def test_parse_treats_a_missing_result_file_as_invalid_output(tmp_path: Path) -> None:
    with pytest.raises(HarnessInvalidOutputError, match="did not write result JSON"):
        _parse(b'{"type":"agent_settled"}', tmp_path)


def test_parse_treats_a_broken_stream_as_invalid_output(tmp_path: Path) -> None:
    with pytest.raises(HarnessInvalidOutputError, match="no usable stream"):
        _parse(b"not json", tmp_path)


async def test_a_pasted_key_renders_under_its_provider(client, druks_db) -> None:
    assert (
        client.post("/api/providers/openai/connection", json={"key": _API_KEY}).status_code == 200
    )
    account = await Account.get_or_create("op@example.com")
    login = await ProviderLogin.get_for_account("openai", account.id)

    auth = PiHarness.auth_file(login)

    assert auth.path == ".pi/agent/auth.json"
    assert json.loads(auth.content) == {"openai": {"type": "api_key", "key": _API_KEY}}
