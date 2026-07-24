import pytest
from druks.harnesses.claude import ClaudeHarness
from druks.harnesses.codex import CodexHarness
from druks.harnesses.exceptions import HarnessError
from druks.harnesses.registry import get_harness_for_model
from druks.user_settings.models import HarnessSettings


def test_fetched_model_list_controls_routing():
    HarnessSettings.require("claude").update(
        models_fetched=[{"id": "claude-fable-5", "label": "Claude Fable 5"}],
    )

    assert get_harness_for_model("claude-fable-5") is ClaudeHarness

    with pytest.raises(HarnessError):
        get_harness_for_model("claude-opus-5")


def test_unfetched_harness_settings_fall_back_to_shipped_models():
    HarnessSettings.require("codex").update(models_fetched=None)
    HarnessSettings.require("claude").update(models_fetched=None)

    assert get_harness_for_model(CodexHarness.models[0]) is CodexHarness
    assert get_harness_for_model(ClaudeHarness.models[0]) is ClaudeHarness


def test_bare_harness_names_route_to_their_harnesses():
    HarnessSettings.require("claude").update(
        models_fetched=[{"id": "claude-fable-5", "label": "Claude Fable 5"}],
    )
    HarnessSettings.require("codex").update(
        models_fetched=[{"id": "gpt-5.5", "label": "GPT 5.5"}],
    )

    assert get_harness_for_model("claude") is ClaudeHarness
    assert get_harness_for_model("codex") is CodexHarness


@pytest.mark.parametrize(
    "model",
    ("llama-3-70b", "claude-opus-5", "gpt-6", "o4-mini"),
)
def test_unlisted_models_raise(model: str):
    HarnessSettings.require("claude").update(models_fetched=None)
    HarnessSettings.require("codex").update(models_fetched=None)

    with pytest.raises(HarnessError):
        get_harness_for_model(model)
