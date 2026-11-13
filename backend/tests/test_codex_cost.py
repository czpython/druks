import json
import subprocess
from pathlib import Path

from druks.harnesses import codex
from druks.harnesses.codex import CodexHarness, read_codex_cost_from_jsonl
from druks.harnesses.datastructures import Manifest
from druks.sandbox.datastructures import HarnessRunResult


def test_read_from_jsonl_handles_event_msg_payload_wrapper(tmp_path: Path) -> None:
    """Regression: newer codex builds wrap token_count events as
    ``{"type":"event_msg","payload":{"type":"token_count","info":{...}}}``.
    The parser must read ``info`` from the nested payload, not the
    top-level event."""
    path = tmp_path / "codex-session.jsonl"
    event = {
        "timestamp": "2026-06-07T14:41:03.746Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "model": "gpt-5.5",
                "total_token_usage": {
                    "input_tokens": 42045,
                    "cached_input_tokens": 2432,
                    "output_tokens": 738,
                    "reasoning_output_tokens": 263,
                    "total_tokens": 42783,
                },
            },
        },
    }
    path.write_text(json.dumps(event) + "\n")

    cost, metadata = read_codex_cost_from_jsonl(path, model="gpt-5.5")

    # 39,613 uncached x $1.25 + 2,432 cached x $0.125 + 738 output x $10, per million.
    # The 263 reasoning tokens are part of the 738 output tokens, so they bill once.
    assert cost == 0.0572
    assert metadata is not None
    assert metadata["input_tokens"] == 42045
    assert metadata["output_tokens"] == 738
    assert metadata["reasoning_output_tokens"] == 263


def _write_token_count_event(path: Path, model: str) -> None:
    event = {
        "timestamp": "2026-06-07T14:41:03.746Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "model": model,
                "total_token_usage": {
                    "input_tokens": 1_000_000,
                    "cached_input_tokens": 0,
                    "output_tokens": 1_000_000,
                    "reasoning_output_tokens": 0,
                    "total_tokens": 2_000_000,
                },
            },
        },
    }
    path.write_text(json.dumps(event) + "\n")


def test_dated_mini_variant_uses_mini_rates_not_generic_gpt5(tmp_path: Path) -> None:
    """Regression: a dated ``gpt-5-mini`` id must fall back onto the mini rates,
    not the overlapping generic ``gpt-5`` prefix that precedes it in the
    default table."""
    path = tmp_path / "codex-session.jsonl"
    _write_token_count_event(path, "gpt-5-mini-2026-01-01")

    cost, metadata = read_codex_cost_from_jsonl(path, model=None)

    assert metadata is not None
    assert metadata["input_per_million_usd"] == 0.25
    assert metadata["cached_input_per_million_usd"] == 0.025
    assert metadata["output_per_million_usd"] == 2.0
    # A recognized model carries no default-rate fallback marker.
    assert "note" not in metadata
    # 1M uncached input at $0.25 + 1M output at $2.00, per-million.
    assert cost == 2.25


def test_dated_nano_variant_uses_nano_rates_not_generic_gpt5(tmp_path: Path) -> None:
    """Regression: a suffixed ``gpt-5-nano`` id must select the nano rates
    rather than the generic ``gpt-5`` rates."""
    path = tmp_path / "codex-session.jsonl"
    _write_token_count_event(path, "gpt-5-nano-2026-01-01")

    cost, metadata = read_codex_cost_from_jsonl(path, model=None)

    assert metadata is not None
    assert metadata["input_per_million_usd"] == 0.05
    assert metadata["cached_input_per_million_usd"] == 0.005
    assert metadata["output_per_million_usd"] == 0.40
    assert "note" not in metadata
    # 1M uncached input at $0.05 + 1M output at $0.40, per-million.
    assert cost == 0.45


def _parse_codex_call(tmp_path: Path, stdout: bytes) -> dict:
    call_dir = tmp_path / "call"
    call_dir.mkdir(exist_ok=True)
    (call_dir / "output.json").write_text("{}")
    # _tee_stream writes the whole stream to stdout.jsonl beside the call's files.
    (call_dir / "stdout.jsonl").write_bytes(stdout)
    harness = CodexHarness(model="openai/gpt-5.5", fast_mode=False, effort=None)
    harness.parse(
        HarnessRunResult(returncode=0, stdout=stdout, stderr=b""),
        artifact_dir=tmp_path,
        run_id="call",
    )
    return json.loads((call_dir / "cost.json").read_text())


def test_parse_prices_turn_completed_without_a_session_file(tmp_path: Path) -> None:
    turn = {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 1_000_000,
            "cached_input_tokens": 0,
            "output_tokens": 1_000_000,
            "reasoning_output_tokens": 0,
        },
    }
    stdout = b'{"type":"turn.started"}\n' + json.dumps(turn).encode() + b"\n"

    cost = _parse_codex_call(tmp_path, stdout)

    # 1M uncached input at $1.25 + 1M output at $10.00, per-million.
    assert cost["cost_usd"] == 11.25
    assert cost["metadata"]["input_tokens"] == 1_000_000
    assert cost["metadata"]["output_tokens"] == 1_000_000


def test_parse_falls_back_to_the_session_file_without_turn_completed(tmp_path: Path) -> None:
    (tmp_path / "call").mkdir()
    _write_token_count_event(tmp_path / "call" / "session.jsonl", "gpt-5.5")
    stdout = b'{"type":"turn.failed","error":{"message":"stream disconnected"}}\n'

    cost = _parse_codex_call(tmp_path, stdout)

    assert cost["cost_usd"] == 11.25


def _compact(event: dict) -> str:
    return json.dumps(event, separators=(",", ":"))


def _session_meta(thread_id: str, parent_thread_id: str) -> str:
    spawn = {"parent_thread_id": parent_thread_id, "depth": 1}
    payload = {"id": thread_id, "source": {"subagent": {"thread_spawn": spawn}}}
    return _compact({"type": "session_meta", "payload": payload})


def _turn_context(model: str) -> str:
    return _compact({"type": "turn_context", "payload": {"model": model}})


def _token_count(tokens: int) -> str:
    usage = {"input_tokens": tokens, "cached_input_tokens": 0, "output_tokens": tokens}
    payload = {"type": "token_count", "info": {"total_token_usage": usage}}
    return _compact({"type": "event_msg", "payload": payload})


_MAIN_TURN = (
    b'{"type":"thread.started","thread_id":"main"}\n'
    b'{"type":"turn.completed","usage":{"input_tokens":1000000,'
    b'"cached_input_tokens":0,"output_tokens":1000000}}\n'
)


def test_parse_adds_a_subagent_priced_at_its_own_model(tmp_path: Path) -> None:
    (tmp_path / "call").mkdir()
    (tmp_path / "call" / "subagents.jsonl").write_text(
        "\n".join(
            [
                _session_meta("child", parent_thread_id="main"),
                _turn_context("gpt-5-mini"),
                _token_count(1_000_000),
            ]
        )
        + "\n"
    )

    cost = _parse_codex_call(tmp_path, _MAIN_TURN)

    # $11.25 for the main thread at gpt-5.5, plus 1M input at $0.25 and
    # 1M output at $2.00 for the subagent at gpt-5-mini.
    assert cost["cost_usd"] == 13.5
    assert cost["metadata"]["input_tokens"] == 2_000_000
    assert cost["metadata"]["output_tokens"] == 2_000_000


def test_parse_skips_a_subagent_of_another_call(tmp_path: Path) -> None:
    (tmp_path / "call").mkdir()
    (tmp_path / "call" / "subagents.jsonl").write_text(
        "\n".join(
            [
                _session_meta("child", parent_thread_id="main"),
                _turn_context("gpt-5.5"),
                _token_count(1_000),
                _session_meta("stranger", parent_thread_id="other"),
                _turn_context("gpt-5.5"),
                _token_count(1_000_000),
            ]
        )
        + "\n"
    )

    cost = _parse_codex_call(tmp_path, _MAIN_TURN)

    # $11.25 for the main thread, plus 1,000 input at $1.25 and 1,000 output
    # at $10.00 per million for the subagent.
    assert cost["cost_usd"] == 11.26125
    assert cost["metadata"]["input_tokens"] == 1_001_000


def test_parse_adds_a_subagent_when_stdout_lost_its_first_event(tmp_path: Path) -> None:
    call_dir = tmp_path / "call"
    call_dir.mkdir()
    (call_dir / "output.json").write_text("{}")
    (call_dir / "subagents.jsonl").write_text(
        "\n".join(
            [
                _session_meta("child", parent_thread_id="main"),
                _turn_context("gpt-5-mini"),
                _token_count(1_000_000),
            ]
        )
        + "\n"
    )
    (call_dir / "stdout.jsonl").write_bytes(_MAIN_TURN)
    turn_completed = _MAIN_TURN.splitlines(keepends=True)[1]

    CodexHarness(model="openai/gpt-5.5", fast_mode=False, effort=None).parse(
        HarnessRunResult(returncode=0, stdout=turn_completed, stderr=b""),
        artifact_dir=tmp_path,
        run_id="call",
    )

    cost = json.loads((call_dir / "cost.json").read_text())
    assert cost["cost_usd"] == 13.5
    assert cost["metadata"]["input_tokens"] == 2_000_000


async def test_the_wrapper_sends_subagent_usage_beside_the_session(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(codex, "get_runs_root", lambda _: str(tmp_path / "runs"))
    sessions = tmp_path / "home" / ".codex" / "sessions" / "2026" / "11" / "10"
    main = [_compact({"type": "session_meta", "payload": {"id": "main", "source": "exec"}})]
    child = [
        _session_meta("child", parent_thread_id="main"),
        _turn_context("gpt-5.5"),
        _token_count(1),
        _compact({"type": "response_item", "payload": {"type": "message"}}),
        _turn_context("gpt-5-mini"),
        _token_count(2),
    ]
    fake_codex = tmp_path / "bin" / "codex"
    fake_codex.parent.mkdir()
    fake_codex.write_text(
        "#!/bin/sh\n"
        f"mkdir -p {sessions}\n"
        f"cat > {sessions}/rollout-main.jsonl <<'EOF'\n" + "\n".join(main) + "\nEOF\n"
        f"cat > {sessions}/rollout-child.jsonl <<'EOF'\n" + "\n".join(child) + "\nEOF\n"
    )
    fake_codex.chmod(0o755)
    invocation = await CodexHarness(
        model="openai/gpt-5.5", fast_mode=False, effort=None
    ).build_invocation(
        Manifest(model="", harness="", mcp_servers=(), skills=()),
        prompt="hello",
        schema={"type": "object"},
        run_id="call",
        ssh_username="exedev",
    )

    subprocess.run(
        invocation.args,
        input=b"",
        env={"HOME": str(tmp_path / "home"), "PATH": f"{fake_codex.parent}:/usr/bin:/bin"},
        check=True,
    )

    call_dir = tmp_path / "runs" / "call"
    assert (call_dir / "session.jsonl").read_text().splitlines() == main
    assert (call_dir / "subagents.jsonl").read_text().splitlines() == [
        child[0],
        child[4],
        child[5],
    ]
