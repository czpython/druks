import pytest
from druks.harnesses.claude import ClaudeHarness
from druks.harnesses.codex import CodexHarness
from druks.harnesses.exceptions import HarnessError
from druks.harnesses.registry import get_harness_for_model


def test_new_model_in_known_namespace_routes_without_a_release():
    # Models absent from the shipped ``models`` tuples still route by namespace,
    # so a provider's new model runs the day it ships.
    assert get_harness_for_model("claude-opus-5") is ClaudeHarness
    assert get_harness_for_model("gpt-6") is CodexHarness
    assert get_harness_for_model("o4-mini") is CodexHarness


def test_family_token_routes_to_its_harness():
    assert get_harness_for_model("claude") is ClaudeHarness
    assert get_harness_for_model("codex") is CodexHarness


def test_unroutable_model_raises():
    with pytest.raises(HarnessError):
        get_harness_for_model("llama-3-70b")
