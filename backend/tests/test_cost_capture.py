import json
from pathlib import Path

import pytest
from druks.durable import AgentCall
from druks.harnesses.artifacts import (
    normalize_token_usage,
    read_cost,
    write_cost,
)
from druks.harnesses.claude import (
    ClaudeHarness,
    collapse_claude_stream,
    extract_claude_cost_from_envelope,
)
from druks.harnesses.codex import CodexHarness
from druks.harnesses.exceptions import (
    HarnessError,
    HarnessSpendLimitError,
    HarnessTimeoutError,
    StreamJsonError,
)
from druks.harnesses.opencode import OpenCodeHarness
from druks.harnesses.pi import PiHarness
from druks.sandbox.datastructures import HarnessRunResult
from druks.testing import seed_call, seed_run
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize


def _model_usage(input_tokens: int, output_tokens: int, cache_write: int, cache_read: int) -> dict:
    return {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "cacheCreationInputTokens": cache_write,
        "cacheReadInputTokens": cache_read,
        "webSearchRequests": 0,
        "costUSD": 0.01,
    }


def test_extract_claude_cost_sums_the_tokens_of_every_model():
    envelope = {
        "type": "result",
        "is_error": False,
        "result": "...",
        "total_cost_usd": 0.0423,
        "model": "claude-opus-4-7",
        "duration_ms": 12345,
        # The top-level loop only; the subagent's haiku tokens are in modelUsage.
        "usage": {"input_tokens": 1500, "output_tokens": 600},
        "modelUsage": {
            "claude-opus-4-7": _model_usage(1500, 600, 100, 0),
            "claude-haiku-4-5": _model_usage(20, 5, 0, 300),
        },
    }

    cost_usd, metadata = extract_claude_cost_from_envelope(envelope)

    assert cost_usd == 0.0423
    assert metadata == {
        "provider": "anthropic",
        "input_tokens": 1520,
        "output_tokens": 605,
        "cache_creation_input_tokens": 100,
        "cache_read_input_tokens": 300,
        "model": "claude-opus-4-7",
        "duration_ms": 12345,
    }


def test_extract_claude_cost_returns_none_when_envelope_lacks_cost():
    cost_usd, metadata = extract_claude_cost_from_envelope({"foo": "bar"})

    assert cost_usd is None
    assert metadata is None


def test_failed_claude_call_writes_cost_before_raising(tmp_path: Path):
    message = (
        "You've hit your org's monthly spend limit · ask your admin to raise it · "
        "your session limit resets 2:20pm"
    )
    stdout = (
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "result": message,
                "total_cost_usd": 190.25,
                "modelUsage": {"claude-opus-4-7": _model_usage(100, 20, 30, 400)},
            }
        ).encode()
        + b"\n"
    )
    harness = ClaudeHarness(model="anthropic/claude-opus-4-7", fast_mode=False, effort=None)

    with pytest.raises(HarnessSpendLimitError):
        harness.parse(
            HarnessRunResult(returncode=1, stdout=stdout, stderr=b""),
            artifact_dir=tmp_path,
            run_id="failed-call",
        )

    cost_usd, metadata = read_cost(tmp_path / "failed-call")
    assert cost_usd == 190.25
    assert metadata == {
        "provider": "anthropic",
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_creation_input_tokens": 30,
        "cache_read_input_tokens": 400,
    }


def test_killed_claude_call_records_the_cost_state_of_its_transcript(tmp_path: Path):
    call = tmp_path / "killed-call"
    call.mkdir()
    cost_state = {
        "type": "cost-state",
        "totalCostUSD": 0.0024,
        "modelUsage": {"claude-haiku-4-5": _model_usage(1215, 188, 10258, 11873)},
    }
    events = [
        {"type": "cost-state", "totalCostUSD": 0.001, "modelUsage": {}},
        cost_state,
        {"type": "assistant", "message": {"content": '{"type":"cost-state"}'}},
    ]
    # Claude Code writes compact JSON.
    (call / "session.jsonl").write_text(
        "".join(json.dumps(event, separators=(",", ":")) + "\n" for event in events)
    )
    stdout = _stream({"type": "assistant", "message": {"id": "msg_1", "content": []}})
    harness = ClaudeHarness(model="anthropic/claude-haiku-4-5", fast_mode=False, effort=None)

    with pytest.raises(HarnessTimeoutError):
        harness.parse(
            HarnessRunResult(returncode=143, stdout=stdout, stderr=b"", timed_out=True),
            artifact_dir=tmp_path,
            run_id="killed-call",
        )

    assert read_cost(call) == (
        0.0024,
        {
            "provider": "anthropic",
            "input_tokens": 1215,
            "output_tokens": 188,
            "cache_creation_input_tokens": 10258,
            "cache_read_input_tokens": 11873,
        },
    )


def test_failed_codex_call_writes_cost_before_raising(tmp_path: Path):
    call_dir = tmp_path / "failed-call"
    call_dir.mkdir()
    event = {
        "timestamp": "2026-06-07T14:41:03.746Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "model": "gpt-5.5",
                "total_token_usage": {
                    "input_tokens": 1_000,
                    "cached_input_tokens": 0,
                    "output_tokens": 1_000,
                    "reasoning_output_tokens": 0,
                    "total_tokens": 2_000,
                },
            },
        },
    }
    (call_dir / "session.jsonl").write_text(json.dumps(event) + "\n")
    harness = CodexHarness(model="openai/gpt-5.5", fast_mode=False, effort=None)

    with pytest.raises(HarnessError):
        harness.parse(
            HarnessRunResult(returncode=1, stdout=b"", stderr=b"request failed\n"),
            artifact_dir=tmp_path,
            run_id="failed-call",
        )

    cost_usd, metadata = read_cost(call_dir)
    assert cost_usd and cost_usd > 0
    assert metadata
    assert metadata["input_tokens"] == 1_000
    assert metadata["output_tokens"] == 1_000


def test_failed_pi_call_writes_cost_before_raising(tmp_path: Path):
    message = {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "stopReason": "error",
            "errorMessage": "OpenAI API error (429): slow down",
            "usage": {
                "input": 100,
                "output": 20,
                "cacheRead": 5,
                "cacheWrite": 2,
                "cost": {"total": 0.75},
            },
        },
    }
    harness = PiHarness(model="openai/gpt-5.5", fast_mode=False, effort=None)

    with pytest.raises(HarnessError):
        harness.parse(
            HarnessRunResult(
                returncode=0,
                stdout=(json.dumps(message) + "\n").encode(),
                stderr=b"",
            ),
            artifact_dir=tmp_path,
            run_id="failed-call",
        )

    cost_usd, metadata = read_cost(tmp_path / "failed-call")
    assert cost_usd == 0.75
    assert metadata
    assert metadata["input_tokens"] == 100
    assert metadata["output_tokens"] == 20


def test_failed_opencode_call_writes_cost_before_raising(tmp_path: Path):
    response = {
        "info": {
            "role": "assistant",
            "providerID": "anthropic",
            "modelID": "claude-sonnet-4-5",
            "cost": 0.75,
            "tokens": {
                "input": 100,
                "output": 20,
                "reasoning": 3,
                "cache": {"read": 5, "write": 2},
            },
            "error": {
                "name": "APIError",
                "data": {"message": "busy", "isRetryable": True},
            },
        },
        "parts": [],
    }
    harness = OpenCodeHarness(
        model="anthropic/claude-sonnet-4-5",
        fast_mode=False,
        effort=None,
    )

    with pytest.raises(HarnessError):
        harness.parse(
            HarnessRunResult(
                returncode=1,
                stdout=(json.dumps(response) + "\n").encode(),
                stderr=b"request failed\n",
            ),
            artifact_dir=tmp_path,
            run_id="failed-call",
        )

    cost_usd, metadata = read_cost(tmp_path / "failed-call")
    assert cost_usd == 0.75
    assert metadata
    assert metadata["input_tokens"] == 100
    assert metadata["output_tokens"] == 20


def test_write_cost_and_read_cost_round_trip(tmp_path: Path):
    write_cost(tmp_path, cost_usd=0.5, metadata={"input_tokens": 100})

    cost_usd, metadata = read_cost(tmp_path)

    assert cost_usd == 0.5
    assert metadata == {"input_tokens": 100}


def test_write_cost_skips_when_nothing_to_write(tmp_path: Path):
    write_cost(tmp_path, cost_usd=None, metadata=None)

    assert not (tmp_path / "cost.json").exists()


def test_read_cost_returns_none_for_corrupt_file(tmp_path: Path):
    (tmp_path / "cost.json").write_text("not json")

    cost_usd, metadata = read_cost(tmp_path)

    assert cost_usd is None
    assert metadata is None


async def test_record_agent_run_cost_persists_to_db(druks_db):
    note = await Note.create(body="cost capture")
    run = await seed_run(druks_db, kind=Summarize.kind, subject=note)
    call = await seed_call(druks_db, run, "summarize", status="running")
    # record_cost flushes the ambient session, so mutate the ambient copy.
    call = await AgentCall.get(druks_db, call.id)
    await call.record_cost(
        cost_usd=1.23,
        cost_metadata={"input_tokens": 200, "model": "claude-opus-4-7"},
    )

    fetched = await AgentCall.get(druks_db, call.id)
    assert fetched is not None
    assert fetched.cost_usd == 1.23
    assert fetched.cost_metadata == {"input_tokens": 200, "model": "claude-opus-4-7"}


async def test_record_agent_run_cost_noop_when_empty(druks_db):
    note = await Note.create(body="empty cost")
    run = await seed_run(druks_db, kind=Summarize.kind, subject=note)
    call = await seed_call(druks_db, run, "summarize", status="running")

    await call.record_cost(cost_usd=None, cost_metadata=None)

    fetched = await AgentCall.get(druks_db, call.id)
    assert fetched is not None
    assert fetched.cost_usd is None
    assert fetched.cost_metadata is None


def test_cost_file_format_is_loadable_json(tmp_path: Path):
    write_cost(tmp_path, cost_usd=0.42, metadata={"input_tokens": 50})

    data = json.loads((tmp_path / "cost.json").read_text())
    assert data == {"cost_usd": 0.42, "metadata": {"input_tokens": 50}}


def test_normalize_token_usage_anthropic_folds_cache_into_input_total():
    metadata = {
        "provider": "anthropic",
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 30,
        "cache_creation_input_tokens": 10,
    }

    canonical = normalize_token_usage(metadata)

    assert canonical == {
        "input_tokens": 140,  # 100 new + 30 cache read + 10 cache write
        "output_tokens": 50,
        "cached_input_tokens": 30,
        "cache_creation_tokens": 10,
        "reasoning_tokens": 0,
        "total_tokens": 190,  # 140 input + 50 output
    }


def test_normalize_token_usage_openai_keeps_input_and_output_inclusive():
    metadata = {
        "provider": "openai",
        "input_tokens": 200,
        "cached_input_tokens": 50,
        "output_tokens": 100,
        "reasoning_output_tokens": 40,
    }

    canonical = normalize_token_usage(metadata)

    assert canonical == {
        "input_tokens": 200,  # unchanged — already inclusive of cached
        "output_tokens": 100,  # already includes the 40 reasoning
        "cached_input_tokens": 50,
        "cache_creation_tokens": 0,
        "reasoning_tokens": 40,
        "total_tokens": 300,
    }


def test_normalize_token_usage_returns_none_for_empty_metadata():
    assert normalize_token_usage({"provider": "anthropic"}) is None
    assert normalize_token_usage({}) is None
    assert normalize_token_usage(None) is None


def test_normalize_token_usage_unknown_provider_uses_canonical_field_names():
    metadata = {
        "provider": "future",
        "input_tokens": 5,
        "output_tokens": 3,
        "reasoning_tokens": 2,
    }

    canonical = normalize_token_usage(metadata)

    assert canonical is not None
    assert canonical["input_tokens"] == 5
    assert canonical["output_tokens"] == 3
    assert canonical["reasoning_tokens"] == 2
    assert canonical["total_tokens"] == 8


def test_normalize_token_usage_coerces_string_numbers_and_skips_garbage():
    metadata = {
        "provider": "anthropic",
        "input_tokens": "100",
        "output_tokens": 50.0,
        "cache_read_input_tokens": None,
        "cache_creation_input_tokens": "garbage",
    }

    canonical = normalize_token_usage(metadata)

    assert canonical is not None
    assert canonical["input_tokens"] == 100  # 100 + 0 (None) + 0 (garbage)
    assert canonical["output_tokens"] == 50


def _stream(*events: dict) -> bytes:
    return b"\n".join(json.dumps(e).encode() for e in events) + b"\n"


def test_collapse_stream_pulls_model_from_init_and_result_from_final_event():
    stream = _stream(
        {"type": "system", "subtype": "init", "model": "claude-opus-4-7"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "thinking…"}]}},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": '{"ok": true}',
            "total_cost_usd": 0.05,
            "duration_ms": 12000,
            "usage": {"input_tokens": 100, "output_tokens": 50},
        },
    )

    envelope = collapse_claude_stream(stream)

    assert envelope["model"] == "claude-opus-4-7"
    assert envelope["result"] == '{"ok": true}'
    assert envelope["total_cost_usd"] == 0.05
    assert envelope["usage"]["input_tokens"] == 100


def test_collapse_stream_tolerates_non_json_lines_mid_stream():
    stream = (
        b'{"type": "system", "subtype": "init", "model": "claude-haiku-4-5"}\n'
        b"not a json line, should be ignored\n"
        b'{"type": "result", "result": "ok", "total_cost_usd": 0.01}\n'
    )

    envelope = collapse_claude_stream(stream)

    assert envelope["model"] == "claude-haiku-4-5"
    assert envelope["result"] == "ok"


def test_collapse_stream_raises_when_no_result_event():
    stream = _stream(
        {"type": "system", "subtype": "init", "model": "claude-haiku-4-5"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "…"}]}},
    )

    try:
        collapse_claude_stream(stream)
    except StreamJsonError as exc:
        assert "result" in str(exc)
    else:
        raise AssertionError("expected StreamJsonError")


def test_collapse_stream_raises_when_nothing_parses():
    try:
        collapse_claude_stream(b"garbage output\nfrom claude\n")
    except StreamJsonError as exc:
        assert "no parseable events" in str(exc)
    else:
        raise AssertionError("expected StreamJsonError")
