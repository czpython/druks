import json

from druks.harnesses.claude import ClaudeHarness
from druks.harnesses.codex import CodexHarness
from druks.user_settings.models import HarnessSettings


def test_claude_parse_maps_ids_and_labels() -> None:
    payload = json.dumps(
        {
            "data": [
                {"id": "claude-fable-5", "display_name": "Claude Fable 5", "type": "model"},
                {"id": "claude-opus-4-8"},
            ],
            "has_more": False,
        }
    )

    parsed = ClaudeHarness._parse_models(payload)

    assert parsed.ok
    assert parsed.models == (
        {"id": "claude-fable-5", "label": "Claude Fable 5"},
        {"id": "claude-opus-4-8", "label": "claude-opus-4-8"},
    )


def test_claude_parse_empty_catalog_is_an_error() -> None:
    parsed = ClaudeHarness._parse_models(json.dumps({"data": []}))

    assert not parsed.ok
    assert parsed.error == "empty_list"


def test_claude_parse_rejects_unexpected_payload() -> None:
    parsed = ClaudeHarness._parse_models(json.dumps({"models": []}))

    assert not parsed.ok
    assert parsed.error == "unexpected_payload"


def test_claude_parse_rejects_non_json() -> None:
    parsed = ClaudeHarness._parse_models("<!doctype html>")

    assert not parsed.ok
    assert parsed.error == "unparseable"


def test_codex_parse_keeps_only_listed_models() -> None:
    payload = json.dumps(
        {
            "models": [
                {
                    "slug": "gpt-5.6-sol",
                    "display_name": "GPT-5.6-Sol",
                    "visibility": "list",
                    "minimal_client_version": "0.144.0",
                    "supported_reasoning_levels": [
                        {"effort": "low"},
                        {"effort": "xhigh"},
                        {"effort": "ultra"},
                    ],
                },
                {
                    "slug": "codex-auto-review",
                    "display_name": "Codex Auto Review",
                    "visibility": "hide",
                },
            ]
        }
    )

    parsed = CodexHarness._parse_models(payload)

    assert parsed.ok
    assert parsed.models == (
        {
            "id": "gpt-5.6-sol",
            "label": "GPT-5.6-Sol",
            "efforts": ["low", "xhigh", "ultra"],
            "minimal_client_version": "0.144.0",
        },
    )


def test_codex_parse_empty_catalog_is_an_error() -> None:
    """A stale-low ``client_version`` yields ``200 {"models": []}`` — that must
    never read as "no models" and wipe the stored list."""
    parsed = CodexHarness._parse_models(json.dumps({"models": []}))

    assert not parsed.ok
    assert parsed.error == "empty_list"


def test_allowed_models_falls_back_to_the_shipped_tuple() -> None:
    settings = HarnessSettings(name="claude")

    assert [m["id"] for m in settings.allowed_models] == list(ClaudeHarness.models)
    assert all(m["label"] == m["id"] for m in settings.allowed_models)


def test_allowed_models_prefers_the_fetched_list() -> None:
    settings = HarnessSettings(name="claude")
    settings.models_fetched = [{"id": "claude-fable-5", "label": "Claude Fable 5", "extra": 1}]

    assert settings.allowed_models == [{"id": "claude-fable-5", "label": "Claude Fable 5"}]
