import pytest
from conftest import connect_harness
from druks.harnesses.claude import ClaudeHarness
from druks.harnesses.codex import CodexHarness

_CODEX_MODEL = CodexHarness.models[0]


@pytest.fixture(autouse=True)
def _connected_harnesses(db_session):
    # build_invocation renders each credential bundle from the DB row and
    # raises when that harness isn't connected.
    connect_harness(ClaudeHarness, {"claudeAiOauth": {"accessToken": "t"}})
    connect_harness(CodexHarness, {"tokens": {"access_token": "t"}})


def _sandbox_config():
    from pathlib import Path

    from druks.harnesses.datastructures import SandboxSettings

    return SandboxSettings(
        service_url="https://sb.test",
        service_token="t",
        service_timeout=30.0,
        image="img",
        claude_config_dir=Path("/home/agent/.claude"),
        codex_config_dir=Path("/home/agent/.codex"),
    )


def test_claude_build_invocation_carries_every_flag():
    """Flag-drop guard: moving argv construction is exactly how
    CLI flags got silently lost before — assert the full surface."""
    import shlex

    from druks.sandbox.datastructures import McpServer

    schema = {"type": "object"}
    server = McpServer(name="github", url="https://api.example/mcp/", bearer_token_env_var="TOK")
    inv = ClaudeHarness(
        model="claude-x",
        fast_mode=True,
        effort="high",
        sandbox=_sandbox_config(),
    ).build_invocation(
        prompt="hello",
        schema=schema,
        run_id="run-1",
        ssh_username="exedev",
        add_dirs=("/work/related/sib",),
        extra_env={"TOK": "secret"},
        mcp_servers=(server,),
    )

    assert inv.name == "claude"
    assert inv.args[:2] == ("sh", "-c")
    wrapper = inv.args[2]
    for token in (
        "claude",
        "--model claude-x",
        "--settings",
        "fastMode",
        "--effort high",
        "--print",
        "--output-format stream-json",
        "--verbose",
        "--json-schema",
        shlex.quote(__import__("json").dumps(schema)),
        "--permission-mode bypassPermissions",
        "--debug-file",
        "/work/runs/run-1/debug.log",
        "--add-dir /work/related/sib",
        "--mcp-config",
        # The session-snapshot wrapper (claude's CODEX_HOME equivalent).
        "find $HOME/.claude/projects",
        "session.jsonl",
    ):
        assert token in wrapper, f"missing from claude argv: {token}"
    # The MCP token rides only in env — never in argv.
    assert "secret" not in wrapper
    assert inv.env == {"TOK": "secret"}
    assert inv.stdin == b"hello"
    assert inv.extra_artifact_filenames == ("debug.log", "session.jsonl")


def test_codex_build_invocation_carries_every_flag():
    """Flag-drop guard, codex side."""
    from druks.sandbox.datastructures import McpServer

    server = McpServer(name="github", url="https://api.example/mcp/", bearer_token_env_var="TOK")
    inv = CodexHarness(
        model=_CODEX_MODEL,
        fast_mode=True,
        effort="high",
        sandbox=_sandbox_config(),
    ).build_invocation(
        prompt="hello",
        schema={"type": "object"},
        run_id="run-1",
        ssh_username="exedev",
        extra_env={"TOK": "secret"},
        mcp_servers=(server,),
    )

    assert inv.name == "codex"
    assert inv.args[:2] == ("sh", "-c")
    wrapper = inv.args[2]
    for token in (
        "codex exec",
        f"--model {_CODEX_MODEL}",
        "features.fast_mode=true",
        "model_reasoning_summary=auto",
        "model_reasoning_effort=high",
        "--json",
        "--cd /home/exedev/work",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "--output-schema",
        "--output-last-message",
        "/home/exedev/work/runs/run-1/output.json",
        # MCP registration + marker-based session capture. No CODEX_HOME
        # override: codex runs against its real ~/.codex (auth, config,
        # skills all live there), and the session is found by marker.
        "mcp_servers.github.url",
        "mcp_servers.github.bearer_token_env_var",
        "touch /home/exedev/work/runs/run-1/session.marker",
        "-newer /home/exedev/work/runs/run-1/session.marker",
        "session.jsonl",
        "refusing to guess",
    ):
        assert token in wrapper, f"missing from codex argv: {token}"
    assert "secret" not in wrapper
    # --output-schema constrains every assistant message, so the prompt
    # suppresses interim messages (narration goes to reasoning) rather than
    # pretending prose can coexist with the flag.
    stdin = (inv.stdin or b"").decode()
    assert stdin.startswith("hello")
    assert "Do not send interim assistant messages" in stdin
    # The per-run home override must never come back silently — it re-homed
    # every piece of codex state (auth, then skills) one regression at a time.
    assert "CODEX_HOME" not in wrapper
    assert inv.env == {"TOK": "secret"}
    assert inv.extra_artifact_filenames == ("output.json", "session.jsonl")


def test_claude_forwards_effort_value_only():
    with_effort = ClaudeHarness(model="claude-x", fast_mode=False, effort="high")._command_args()
    assert with_effort[with_effort.index("--effort") + 1] == "high"

    no_effort = ClaudeHarness(model="claude-x", fast_mode=False, effort=None)._command_args()
    assert "--effort" not in no_effort


def test_codex_emits_summary_effort_and_json_stream():
    flags = CodexHarness(
        model=_CODEX_MODEL,
        fast_mode=False,
        effort="high",
    )._prompt_flags()
    assert "model_reasoning_summary=auto" in flags
    assert "model_reasoning_effort=high" in flags
    assert "--json" in flags  # streams the rollout to stdout for the live tail


def test_codex_mcp_flags_register_server_with_env_var_token():
    from druks.sandbox.datastructures import McpServer

    server = McpServer(name="github", url="https://api.example/mcp/", bearer_token_env_var="GH_TOK")
    flags = CodexHarness(model=_CODEX_MODEL, fast_mode=False, effort=None)._mcp_flags((server,))
    assert 'mcp_servers.github.url="https://api.example/mcp/"' in flags
    assert 'mcp_servers.github.bearer_token_env_var="GH_TOK"' in flags
    # The token value is never named here — only the env-var name.
    assert all("GH_TOK" not in f or "bearer_token_env_var" in f for f in flags)


def test_claude_mcp_flags_emit_env_ref_header_not_literal_token():
    import json

    from druks.sandbox.datastructures import McpServer

    server = McpServer(name="github", url="https://api.example/mcp/", bearer_token_env_var="GH_TOK")
    flags = ClaudeHarness(model="claude-x", fast_mode=False, effort=None)._mcp_flags((server,))
    assert flags[0] == "--mcp-config"
    cfg = json.loads(flags[1])["mcpServers"]["github"]
    assert cfg["url"] == "https://api.example/mcp/"
    # Header references the env var; the token is resolved by claude at runtime.
    assert cfg["headers"]["Authorization"] == "Bearer ${GH_TOK}"

    # No servers → no flags, on both harnesses.
    assert ClaudeHarness(model="x", fast_mode=False, effort=None)._mcp_flags(()) == ()
    assert CodexHarness(model=_CODEX_MODEL, fast_mode=False, effort=None)._mcp_flags(()) == ()


# Workspace scaffolding tests (add_dirs dedup, GitHub MCP injection) live in
# test_build_workspace.py — that behavior is BuildWorkspace's, not the base sandbox's.
