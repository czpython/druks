import json
from pathlib import Path

from druks.harnesses.codex import read_codex_cost_from_jsonl


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

    assert cost is not None and cost > 0
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
