import json
import os
import subprocess
import sys

import pytest
from druks.harnesses.claude import ClaudeHarness
from druks.harnesses.codex import CodexHarness
from druks.harnesses.config import AgentConfig
from druks.harnesses.constants import CREDENTIAL_VARIABLES
from druks.harnesses.opencode import OpenCodeHarness
from druks.harnesses.pi import PiHarness


@pytest.mark.parametrize(
    ("harness", "model", "billing", "selected"),
    [
        (ClaudeHarness, "anthropic/claude-opus-4-7", "subscription", "ANTHROPIC_AUTH_TOKEN"),
        (ClaudeHarness, "anthropic/claude-opus-4-7", "api_key", "ANTHROPIC_API_KEY"),
        (CodexHarness, "openai/gpt-5.5", "subscription", "CODEX_SUBSCRIPTION_TOKEN"),
        (CodexHarness, "openai/gpt-5.5", "api_key", "CODEX_API_KEY"),
        (OpenCodeHarness, "anthropic/claude-sonnet-4-5", "api_key", "ANTHROPIC_API_KEY"),
        (OpenCodeHarness, "openai/gpt-5.5", "api_key", "OPENAI_API_KEY"),
        (PiHarness, "anthropic/claude-sonnet-4-5", "api_key", "ANTHROPIC_API_KEY"),
        (PiHarness, "openai/gpt-5.5", "api_key", "OPENAI_API_KEY"),
    ],
)
def test_child_keeps_the_selected_placeholder_and_removes_competing_credentials(
    harness, model, billing, selected
):
    config = AgentConfig(
        harness_class=harness,
        model=model,
        subscription=None,
        api_key=None,
        secrets={},
        secret_refs=[],
        # An empty identity must not read as a key: the billing selects the variable.
        identity={},
        billing=billing,
        effort="high",
        timeout=60,
        fast_mode=False,
    )
    credentials = dict.fromkeys(CREDENTIAL_VARIABLES, "foreign-key")
    credentials[selected] = "issued-placeholder"
    command = config.get_command(
        (sys.executable, "-c", "import json, os; print(json.dumps(dict(os.environ)))")
    )

    result = subprocess.run(
        command,
        env={"PATH": os.defpath, "MCP_GITHUB_TOKEN": "mcp-placeholder", **credentials},
        capture_output=True,
        text=True,
        check=True,
    )

    environment = json.loads(result.stdout)
    assert {name: environment[name] for name in credentials if name in environment} == {
        selected: "issued-placeholder"
    }
    assert environment["MCP_GITHUB_TOKEN"] == "mcp-placeholder"
