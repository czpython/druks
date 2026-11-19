import json
import shlex
import subprocess
import time
from pathlib import Path

import druks.harnesses.base
import pytest
from drukbox_sdk import Secret
from druks.harnesses.base import Harness
from druks.harnesses.claude import ClaudeHarness
from druks.harnesses.codex import CodexHarness
from druks.harnesses.constants import CLAUDE_STRUCTURED_OUTPUT_INSTRUCTION
from druks.harnesses.datastructures import Manifest
from druks.sandbox.datastructures import McpServer

# A manifest for a call with no skills.
_MANIFEST = Manifest(model="", harness="", mcp_servers=(), skills=())


_CODEX_MODEL = CodexHarness.default_model
_HOOK = Harness.after_agent_hook_path("/home/exedev/work/runs/run-1")
_HOOK_BODY = (
    Path(druks.harnesses.base.__file__).parent / "hooks" / "druks-after-agent.sh"
).read_text()
_NO_WAITING_HOOK = "/home/exedev/work/runs/run-1/druks-no-waiting.sh"
_NO_WAITING_HOOK_BODY = (
    Path(druks.harnesses.base.__file__).parent / "hooks" / "druks-no-waiting.sh"
).read_text()
_NO_WAITING_ENTRY = {
    "matcher": "Bash",
    "hooks": [{"type": "command", "command": f"sh {_NO_WAITING_HOOK}"}],
}


def _written(wrapper: str) -> dict[str, str]:
    parts = shlex.split(wrapper)
    return {parts[i + 1]: parts[i - 1] for i, part in enumerate(parts) if part == ">"}


async def test_claude_build_invocation_carries_every_flag():
    """The Claude invocation carries the complete supported flag set."""
    schema = {"type": "object"}
    server = McpServer(name="github", url="https://api.example/mcp/", bearer_token_env_var="TOK")
    inv = await ClaudeHarness(
        model="anthropic/claude-x",
        fast_mode=True,
        effort="high",
    ).build_invocation(
        _MANIFEST,
        identity={"email": "op@example.com", "scopes": ["user:inference"]},
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
        shlex.quote(json.dumps(schema)),
        # A structured run says how to submit, so the agent submits once.
        f"--append-system-prompt {shlex.quote(CLAUDE_STRUCTURED_OUTPUT_INSTRUCTION)}",
        "--permission-mode bypassPermissions",
        "--debug-file",
        "/work/runs/run-1/debug.log",
        "--disallowedTools",
        "CronCreate",
        "CronDelete",
        "CronList",
        "Monitor",
        "ScheduleWakeup",
        "--add-dir /work/related/sib",
        "--mcp-config",
        "--session-id run-1",
        # The wrapper outlives the kill and copies the transcript by session id.
        "trap : TERM",
        "cp $HOME/.claude/projects/*/run-1.jsonl ",
        "/work/runs/run-1/session.jsonl 2>/dev/null",
    ):
        assert token in wrapper, f"missing from claude argv: {token}"
    # The MCP token rides only in env — never in argv.
    assert "secret" not in wrapper
    assert "druks-after-agent" not in wrapper
    assert inv.env == {"TOK": "secret"}
    assert inv.stdin == b"hello"
    assert inv.extra_artifact_filenames == ("debug.log", "session.jsonl")


async def test_claude_refuses_the_first_result_of_a_hooked_call():
    inv = await ClaudeHarness(
        model="anthropic/claude-x", fast_mode=True, effort=None
    ).build_invocation(
        _MANIFEST,
        prompt="hello",
        schema={"type": "object"},
        run_id="run-1",
        ssh_username="exedev",
        timeout=900,
        hooks=("after_agent",),
    )

    parts = shlex.split(inv.args[2])
    assert json.loads(parts[parts.index("--settings") + 1]) == {
        "fastMode": True,
        "hooks": {
            "PreToolUse": [
                _NO_WAITING_ENTRY,
                {
                    "matcher": "StructuredOutput",
                    "hooks": [{"type": "command", "command": f"sh {_HOOK}", "timeout": 900}],
                },
            ]
        },
    }
    assert _written(inv.args[2]) == {
        _NO_WAITING_HOOK: _NO_WAITING_HOOK_BODY,
        _HOOK: _HOOK_BODY,
    }


async def test_claude_stages_the_no_waiting_hook_before_every_call():
    invocation = await ClaudeHarness(
        model="anthropic/claude-x", fast_mode=False, effort=None
    ).build_invocation(
        _MANIFEST,
        prompt="hello",
        schema={"type": "object"},
        run_id="run-1",
        ssh_username="exedev",
    )

    parts = shlex.split(invocation.args[2])
    assert json.loads(parts[parts.index("--settings") + 1]) == {
        "hooks": {"PreToolUse": [_NO_WAITING_ENTRY]}
    }
    assert _written(invocation.args[2]) == {_NO_WAITING_HOOK: _NO_WAITING_HOOK_BODY}
    assert parts.index(_NO_WAITING_HOOK) < parts.index("claude")


@pytest.fixture
async def ask_no_waiting_hook(tmp_path: Path):
    """Ask the staged no-waiting hook about a Bash command and return its reply."""
    invocation = await ClaudeHarness(
        model="anthropic/claude-x", fast_mode=False, effort=None
    ).build_invocation(
        _MANIFEST,
        prompt="hello",
        schema={"type": "object"},
        run_id="run-1",
        ssh_username="exedev",
    )
    hook = tmp_path / "druks-no-waiting.sh"
    hook.write_text(_written(invocation.args[2])[_NO_WAITING_HOOK])

    def ask(command: str, description: str = "Run it") -> str:
        call = {
            "session_id": "session-1",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"description": description, "command": command, "timeout": 120000},
        }
        process = subprocess.run(
            ["sh", hook],
            input=json.dumps(call),
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        assert process.stderr == ""
        return process.stdout

    return ask


@pytest.mark.parametrize(
    "command",
    [
        # A sleep that waits out CI before it reads the checks.
        "sleep 300; gh pr checks",
        "sleep 121",
        "sleep 120.5",
        "sleep 5m",
        "sleep 2.5m",
        "sleep 1h",
        "sleep 1m 61s",
        "sleep $delay",
        "sleep infinity",
        # The lengths of every sleep in the command add up.
        "sleep 110 && sleep 110",
        "sleep 90; gh pr checks 7; sleep 90",
        "sleep 1m && sleep 1m && sleep 1m",
        "--watch, whatever the tool.",
        "gh pr checks 7 --watch",
        "git push && gh pr checks 7 && sleep 600",
        "(sleep 400) && gh pr checks 7",
        "sleep\t300",
        "sleep \\\n  300",
        'git commit -m "Fix the \\"stuck\\" build" && sleep 300',
        'echo "Café ☕"; sleep 300',
        'git commit -m "Never sleep 300 in a turn"',
    ],
)
async def test_the_no_waiting_hook_denies_a_command_that_waits(ask_no_waiting_hook, command):
    reply = json.loads(ask_no_waiting_hook(command))["hookSpecificOutput"]

    assert (reply["hookEventName"], reply["permissionDecision"]) == ("PreToolUse", "deny")


async def test_the_no_waiting_hook_names_the_rule_and_the_way_out_of_quoted_text(
    ask_no_waiting_hook,
):
    reply = json.loads(ask_no_waiting_hook('git commit -m "Never sleep 300"'))

    reason = reply["hookSpecificOutput"]["permissionDecisionReason"]
    assert "Druks" in reason
    assert "no-waiting rule" in reason
    assert "file-edit tool" in reason
    assert "rephrase" in reason


@pytest.mark.parametrize(
    "command",
    [
        "sleep 2",
        "sleep 120",
        "sleep 2m",
        "sleep 1m 60s",
        "sleep 60 && sleep 60",
        "sleep 5 && gh pr checks 7",
        "gh pr checks",
        "gh run view --log-failed",
        "gh run watch 123",
        "for i in 1 2 3; do gh run view $i --log-failed; done",
        "for i in 1 2 3; do curl -sf localhost:8000 && break; sleep 2; done",
        "for f in a b; do echo $f; done; gh pr checks 7; sleep 3",
        'git commit -m "Add support for retries" && git push && sleep 10 && gh pr checks 7',
        'gh pr create --title "Wait for CI" --body "See the plan." && sleep 3 && gh pr checks 7',
        "cat > notes.md <<'EOF'\nWait for the build\nEOF\nsleep 5 && gh run list --limit 1",
        'git commit -m "Retry for flaky tests; do not wait" && sleep 5 && gh pr checks 7',
        "ng test --watch=false",
        'git commit -m "Keep the sleep short"',
        'echo "Café ☕"',
        "uv run pytest backend/",
    ],
)
async def test_the_no_waiting_hook_lets_other_commands_run(ask_no_waiting_hook, command):
    assert ask_no_waiting_hook(command) == ""


async def test_the_no_waiting_hook_reads_only_the_command(ask_no_waiting_hook):
    assert ask_no_waiting_hook("gh pr checks 7", description='Run "command": "sleep 300"') == ""


async def test_the_no_waiting_hook_never_runs_the_command(ask_no_waiting_hook, tmp_path: Path):
    marker = tmp_path / "ran"

    assert ask_no_waiting_hook(f"echo ran > {marker}; sleep 300")
    assert ask_no_waiting_hook(f"echo ran > {marker}") == ""
    assert not marker.exists()


async def test_codex_build_invocation_carries_every_flag():
    """The Codex invocation carries the complete supported flag set."""
    server = McpServer(name="github", url="https://api.example/mcp/", bearer_token_env_var="TOK")
    inv = await CodexHarness(
        model=_CODEX_MODEL,
        fast_mode=True,
        effort="high",
    ).build_invocation(
        _MANIFEST,
        identity={"email": "op@example.com", "account_id": "acc-1"},
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
        "--model gpt-5.5",
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
        # Codex uses its sandbox home for auth, config, skills, and session data.
        "mcp_servers.github.url",
        "mcp_servers.github.bearer_token_env_var",
        "touch /home/exedev/work/runs/run-1/session.marker",
        "trap : TERM",
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
    assert "CODEX_HOME" not in wrapper
    assert "druks-after-agent" not in wrapper
    assert inv.env == {"TOK": "secret"}
    assert inv.extra_artifact_filenames == ("output.json", "session.jsonl", "subagents.jsonl")


async def test_codex_blocks_the_first_stop_of_a_hooked_call():
    inv = await CodexHarness(model=_CODEX_MODEL, fast_mode=False, effort=None).build_invocation(
        _MANIFEST,
        prompt="hello",
        schema={"type": "object"},
        run_id="run-1",
        ssh_username="exedev",
        timeout=900,
        hooks=("after_agent",),
    )

    parts = shlex.split(inv.args[2])
    hook = parts.index("--dangerously-bypass-hook-trust")
    assert parts[hook - 2 : hook] == [
        "-c",
        f'hooks.Stop=[{{hooks=[{{type="command",command="sh {_HOOK}",timeout=900}}]}}]',
    ]
    assert _written(inv.args[2])[_HOOK] == _HOOK_BODY


def test_the_hook_prints_the_answer_once(tmp_path: Path):
    hook = tmp_path / "druks-after-agent.sh"
    hook.write_text(_HOOK_BODY)

    waiting = subprocess.Popen(["sh", hook], stdout=subprocess.PIPE, text=True)
    while not (tmp_path / "after_agent.request").exists():
        time.sleep(0.01)
    (tmp_path / "after_agent.answer").write_text("Run the tests.")
    assert waiting.communicate(timeout=5)[0] == "Run the tests."

    second = subprocess.run(["sh", hook], capture_output=True, text=True, check=True, timeout=5)
    assert second.stdout == ""


def test_claude_turns_a_nudge_into_a_denied_result():
    assert ClaudeHarness.get_after_agent_reply("") == ""
    reply = json.loads(ClaudeHarness.get_after_agent_reply("Run the tests."))
    assert reply["hookSpecificOutput"] == {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "Run the tests.",
    }


def test_codex_turns_a_nudge_into_a_blocked_stop():
    assert CodexHarness.get_after_agent_reply("") == ""
    reply = json.loads(CodexHarness.get_after_agent_reply("Run the tests."))
    assert reply == {"decision": "block", "reason": "Run the tests."}


async def test_claude_reads_its_key_from_a_placeholder_in_the_vm():
    # Drukbox delivers the key as a placeholder under ANTHROPIC_API_KEY. The
    # invocation carries no key and no credentials file.
    assert ClaudeHarness.get_secrets("anthropic", "sk-secret") == {
        "anthropic": Secret(
            "sk-secret",
            host="api.anthropic.com",
            auth_variable="ANTHROPIC_API_KEY",
            auth_header="x-api-key",
            auth_prefix="",
        )
    }
    inv = await ClaudeHarness(
        model="anthropic/claude-x", fast_mode=False, effort=None
    ).build_invocation(
        _MANIFEST,
        prompt="hello",
        schema={"type": "object"},
        run_id="run-1",
        ssh_username="exedev",
        extra_env={"TOK": "secret"},
    )
    assert inv.env == {"TOK": "secret"}
    assert not any(file.path == ".claude/.credentials.json" for file in inv.credentials.home)
    assert "sk-secret" not in inv.args[2]


async def test_codex_reads_its_key_from_a_placeholder_in_the_vm():
    # Drukbox delivers the key as a placeholder under CODEX_API_KEY, the one
    # variable codex exec reads. The invocation carries no key and no auth.json.
    assert CodexHarness.get_secrets("openai", "sk-secret") == {
        "openai": Secret(
            "sk-secret",
            host="api.openai.com",
            auth_variable="CODEX_API_KEY",
            auth_header="Authorization",
            auth_prefix="Bearer ",
        )
    }
    inv = await CodexHarness(model=_CODEX_MODEL, fast_mode=False, effort=None).build_invocation(
        _MANIFEST,
        prompt="hello",
        schema={"type": "object"},
        run_id="run-1",
        ssh_username="exedev",
    )
    assert not any(file.path == ".codex/auth.json" for file in inv.credentials.home)
    assert inv.env is None
    assert "sk-secret" not in inv.args[2]


def test_claude_forwards_effort_value_only():
    with_effort = ClaudeHarness(
        model="anthropic/claude-x", fast_mode=False, effort="high"
    )._command_args({})
    assert with_effort[with_effort.index("--effort") + 1] == "high"

    no_effort = ClaudeHarness(
        model="anthropic/claude-x", fast_mode=False, effort=None
    )._command_args({})
    assert "--effort" not in no_effort


def test_codex_emits_summary_effort_and_json_stream():
    flags = CodexHarness(
        model=_CODEX_MODEL,
        fast_mode=False,
        effort="high",
    )._prompt_flags(include_apps=False)
    assert "model_reasoning_summary=auto" in flags
    assert "model_reasoning_effort=high" in flags
    assert "--json" in flags  # streams the rollout to stdout for the live tail


def test_codex_runs_max_effort_as_xhigh():
    # Codex's reasoning scale ends at xhigh; Druks's max lands there.
    flags = CodexHarness(model=_CODEX_MODEL, fast_mode=False, effort="max")._prompt_flags(
        include_apps=False
    )
    assert "model_reasoning_effort=xhigh" in flags
    assert "model_reasoning_effort=max" not in flags


def test_claude_forwards_max_effort_as_is():
    args = ClaudeHarness(model="anthropic/claude-x", fast_mode=False, effort="max")._command_args(
        {}
    )
    assert args[args.index("--effort") + 1] == "max"


def test_codex_mcp_flags_register_server_with_env_var_token():
    server = McpServer(name="github", url="https://api.example/mcp/", bearer_token_env_var="GH_TOK")
    flags = CodexHarness(model=_CODEX_MODEL, fast_mode=False, effort=None)._mcp_flags((server,))
    assert 'mcp_servers.github.url="https://api.example/mcp/"' in flags
    assert 'mcp_servers.github.bearer_token_env_var="GH_TOK"' in flags
    assert "mcp_optional_startup_grace_ms=0" in flags
    # The token value is never named here — only the env-var name.
    assert all("GH_TOK" not in f or "bearer_token_env_var" in f for f in flags)


def test_claude_mcp_flags_emit_env_ref_header_not_literal_token():
    server = McpServer(name="github", url="https://api.example/mcp/", bearer_token_env_var="GH_TOK")
    flags = ClaudeHarness(model="anthropic/claude-x", fast_mode=False, effort=None)._mcp_flags(
        (server,)
    )
    assert flags[0] == "--mcp-config"
    cfg = json.loads(flags[1])["mcpServers"]["github"]
    assert cfg["url"] == "https://api.example/mcp/"
    # Header references the env var; the token is resolved by claude at runtime.
    assert cfg["headers"]["Authorization"] == "Bearer ${GH_TOK}"

    # No servers → no flags, on both harnesses.
    assert ClaudeHarness(model="x", fast_mode=False, effort=None)._mcp_flags(()) == ()
    assert CodexHarness(model=_CODEX_MODEL, fast_mode=False, effort=None)._mcp_flags(()) == ()
