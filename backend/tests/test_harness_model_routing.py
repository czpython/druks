import pytest
from fastapi import HTTPException

from druks.harnesses.claude import ClaudeHarness
from druks.harnesses.codex import CodexHarness
from druks.harnesses.exceptions import HarnessError
from druks.harnesses.registry import get_harness_for_model
from druks.user_settings.models import HarnessSettings
from druks.user_settings.routes import _validate_model


def test_shipped_tuple_fallback_routes_shipped_models(db_session):
    assert get_harness_for_model("claude-opus-4-7") is ClaudeHarness
    assert get_harness_for_model("gpt-5.5") is CodexHarness


def test_fetched_list_routes_provider_models(db_session):
    HarnessSettings.require("claude").models_fetched = [
        {"id": "claude-fable-5", "label": "Claude Fable 5"}
    ]
    db_session.flush()

    assert get_harness_for_model("claude-fable-5") is ClaudeHarness


def test_bare_harness_name_routes(db_session):
    assert get_harness_for_model("claude") is ClaudeHarness
    assert get_harness_for_model("codex") is CodexHarness


def test_unknown_model_raises_harness_error(db_session):
    with pytest.raises(HarnessError):
        get_harness_for_model("llama-3-70b")
    with pytest.raises(HarnessError):
        get_harness_for_model("claude-opus-99")


def test_settings_reject_model_missing_from_lists_returns_422(db_session):
    with pytest.raises(HTTPException) as error:
        _validate_model("llama-3-70b")

    assert error.value.status_code == 422
    assert error.value.detail == "No installed harness runs model 'llama-3-70b'."
