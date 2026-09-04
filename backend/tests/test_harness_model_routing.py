import pytest
from druks.harnesses.claude import ClaudeHarness
from druks.harnesses.codex import CodexHarness
from druks.harnesses.exceptions import UnknownModelError
from druks.harnesses.models import ProviderSubscription
from druks.harnesses.opencode import OpenCodeHarness
from druks.harnesses.pi import PiHarness
from druks.harnesses.registry import get_harness_for_model


def test_the_namespace_selects_the_first_harness_with_its_provider():
    assert get_harness_for_model("anthropic/claude-opus-4-7") is ClaudeHarness
    assert get_harness_for_model("openai/gpt-5.5") is CodexHarness


def test_a_subscription_selects_its_vendors_own_cli():
    # Only the vendor's own CLI runs a subscription; a key-only CLI never does.
    assert ProviderSubscription(provider="anthropic").get_harness() is ClaudeHarness
    assert ProviderSubscription(provider="openai").get_harness() is CodexHarness
    assert not any(
        harness.accepts(ProviderSubscription(provider="anthropic"))
        for harness in (OpenCodeHarness, PiHarness)
    )
    with pytest.raises(UnknownModelError, match="No installed harness runs a xai subscription"):
        ProviderSubscription(provider="xai").get_harness()


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
