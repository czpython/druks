import pytest
from druks.harnesses.claude import ClaudeHarness
from druks.harnesses.codex import CodexHarness
from druks.harnesses.exceptions import UnknownModelError
from druks.harnesses.opencode import OpenCodeHarness
from druks.harnesses.registry import get_harness_for_model
from druks.user_settings.models import HarnessSettings
from druks.user_settings.routes import update_harness_settings
from druks.user_settings.schemas import HarnessUpdate
from fastapi import HTTPException


def test_the_namespace_selects_the_first_harness_with_its_provider():
    assert get_harness_for_model("anthropic/claude-opus-4-7") is ClaudeHarness
    assert get_harness_for_model("openai-codex/gpt-5.5") is CodexHarness
    assert get_harness_for_model("openai/gpt-5.5") is OpenCodeHarness


@pytest.mark.parametrize(
    ("model", "message"),
    [
        (
            "claude-opus-4-7",
            "No installed harness runs model 'claude-opus-4-7'; a model id is 'provider/model'.",
        ),
        (
            "llama/3-70b",
            "No installed harness runs model 'llama/3-70b'; a model id is 'provider/model'.",
        ),
    ],
)
def test_a_bare_or_unknown_model_raises(model, message):
    with pytest.raises(UnknownModelError) as error:
        get_harness_for_model(model)
    assert str(error.value) == message


async def test_settings_reject_another_harness_model_with_422(druks_db):
    settings = await HarnessSettings.get_registered("claude")
    original_model = settings.model

    with pytest.raises(HTTPException) as error:
        await update_harness_settings(
            name="claude", body=HarnessUpdate(model="openai-codex/gpt-5.5")
        )

    assert error.value.status_code == 422
    assert error.value.detail == "'openai-codex/gpt-5.5' is not a claude model."
    assert settings.model == original_model
