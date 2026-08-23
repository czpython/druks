from typing import Any, Literal

from pydantic import BaseModel, Field

from druks.apps.config import resolve_app_config
from druks.prompts import render_prompt
from druks.sandbox.datastructures import Profile

GateValue = Literal["human", "none"]
PlanGate = Literal["human", "machine", "machine_then_human", "adaptive"]


class Gates(BaseModel):
    # Each approval gate's routing, grouped so config.yml reads them apart from
    # on_approval. None defers to the defaults resolved by RepoPolicy's gate methods.
    model_config = {"frozen": True, "extra": "forbid"}

    plan_approval: PlanGate | None = None
    implementation_approval: GateValue | None = None


class VerificationProfile(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}

    test_commands: tuple[str, ...] = ()
    lint_commands: tuple[str, ...] = ()
    typecheck_commands: tuple[str, ...] = ()

    def get_commands(self, *, detected: dict[str, Any]) -> dict[str, Any]:
        """These commands, each paired with the CI check the profiler detected for it."""
        checks = {
            entry["command"]: entry["ci_check"]
            for entries in detected.values()
            for entry in entries
        }
        return {
            key: [{"command": command, "ci_check": checks.get(command)} for command in commands]
            for key, commands in (
                ("test_commands", self.test_commands),
                ("lint_commands", self.lint_commands),
                ("typecheck_commands", self.typecheck_commands),
            )
        }


class RepoPolicy(BaseModel):
    """The operator's ``.druks/ship/config.yml``, validated whole so a typo'd
    key fails loud at resolution."""

    model_config = {"frozen": True, "extra": "forbid"}

    gates: Gates = Field(default_factory=Gates)
    sandbox: Profile = Field(default_factory=Profile)
    on_approval: Literal["merge", "none"] = "merge"
    delete_branch: bool = True
    # Operator-pinned verification commands. None → the repo profiler's
    # detected commands stand; an explicit (even empty) profile replaces them.
    verification: VerificationProfile | None = None

    @classmethod
    async def resolve(cls, repo: str | None) -> "RepoPolicy":
        return await resolve_app_config("ship", repo=repo, model=cls)

    def plan_approval_gate(self, workflow_setting: PlanGate) -> PlanGate:
        return self.gates.plan_approval or workflow_setting

    def implementation_approval_gate(self) -> GateValue:
        return self.gates.implementation_approval or "human"

    async def verification_block(self, *, profile: dict[str, Any], repo: str | None) -> str:
        # The agent-facing verification guidance: the profile's effective
        # commands plus this repo's sandbox env keys. ``profile`` is {} until the
        # repo profiler has run — the no-commands branch then carries the "don't
        # invent verification commands" guardrail.
        verification = profile.get("verification") or {}
        sections = [
            {"label": "Lint", "command_entries": verification.get("lint_commands", [])},
            {
                "label": "Typecheck",
                "command_entries": verification.get("typecheck_commands", []),
            },
            {"label": "Tests", "command_entries": verification.get("test_commands", [])},
        ]
        body = await render_prompt(
            "ship/verification_block.md",
            repo=repo,
            sections=sections,
            has_commands=any(section["command_entries"] for section in sections),
            sandbox_env_keys=sorted(self.sandbox.env),
        )
        return body.rstrip() + "\n"
