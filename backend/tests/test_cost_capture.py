import json
from pathlib import Path

from druks.durable import AgentCall
from druks.harnesses.artifacts import (
    normalize_token_usage,
    read_cost,
    write_cost,
)
from druks.harnesses.claude import (
    StreamJsonError,
    collapse_claude_stream,
    extract_claude_cost_from_envelope,
)
from druks.testing import seed_call, seed_run
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize


def test_extract_claude_cost_pulls_total_and_usage():
    envelope = {
        "type": "result",
        "is_error": False,
        "result": "...",
        "total_cost_usd": 0.0423,
        "model": "claude-opus-4-7",
        "duration_ms": 12345,
        "usage": {
            "input_tokens": 1500,
            "output_tokens": 600,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 0,
        },
    }

    cost_usd, metadata = extract_claude_cost_from_envelope(envelope)

    assert cost_usd == 0.0423
    assert metadata is not None
    assert metadata["input_tokens"] == 1500
    assert metadata["output_tokens"] == 600
    assert metadata["cache_creation_input_tokens"] == 100
    assert metadata["model"] == "claude-opus-4-7"
    assert metadata["provider"] == "anthropic"
    assert metadata["duration_ms"] == 12345


def test_extract_claude_cost_returns_none_when_envelope_lacks_cost():
    cost_usd, metadata = extract_claude_cost_from_envelope({"foo": "bar"})

    assert cost_usd is None
    assert metadata is None


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


def test_record_agent_run_cost_persists_to_db(druks_db):
    note = Note.create(body="cost capture")
    run = seed_run(druks_db, kind=Summarize.kind, subject=note)
    call = seed_call(druks_db, run, "summarize", status="running")
    call.record_cost(
        cost_usd=1.23,
        cost_metadata={"input_tokens": 200, "model": "claude-opus-4-7"},
    )

    fetched = AgentCall.get(call.id)
    assert fetched is not None
    assert fetched.cost_usd == 1.23
    assert fetched.cost_metadata == {"input_tokens": 200, "model": "claude-opus-4-7"}


def test_record_agent_run_cost_noop_when_empty(druks_db):
    note = Note.create(body="empty cost")
    run = seed_run(druks_db, kind=Summarize.kind, subject=note)
    call = seed_call(druks_db, run, "summarize", status="running")

    call.record_cost(cost_usd=None, cost_metadata=None)

    fetched = AgentCall.get(call.id)
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


def test_normalize_token_usage_openai_keeps_input_inclusive_and_adds_reasoning():
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
        "output_tokens": 140,  # 100 visible + 40 reasoning
        "cached_input_tokens": 50,
        "cache_creation_tokens": 0,
        "reasoning_tokens": 40,
        "total_tokens": 340,
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
