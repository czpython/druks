import pytest
from druks.harnesses.claude import ClaudeHarness
from druks.harnesses.codex import CodexHarness
from druks.harnesses.exceptions import UnknownModelError
from druks.harnesses.models import ProviderLogin
from druks.harnesses.opencode import OpenCodeHarness
from druks.harnesses.registry import get_harness_for_model


def test_the_namespace_selects_the_first_harness_with_its_provider():
    assert get_harness_for_model("anthropic/claude-opus-4-7") is ClaudeHarness
    assert get_harness_for_model("openai-codex/gpt-5.5") is CodexHarness
    assert get_harness_for_model("openai/gpt-5.5") is OpenCodeHarness


def test_a_login_selects_among_the_harnesses_with_its_provider():
    assert ProviderLogin(provider="anthropic", kind="oauth").get_harness() is ClaudeHarness
    assert ProviderLogin(provider="anthropic", kind="api_key").get_harness() is OpenCodeHarness
    with pytest.raises(UnknownModelError, match="anthropic on a device login"):
        ProviderLogin(provider="anthropic", kind="device").get_harness()


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
