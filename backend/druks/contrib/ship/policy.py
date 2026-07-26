from typing import Any, Literal

from pydantic import BaseModel, Field

from druks.extensions.config import resolve_extension_config
from druks.prompts import render_prompt
from druks.sandbox.datastructures import Profile

GateValue = Literal["human", "none"]


class Gates(BaseModel):
    # Whether each human gate parks for approval, grouped so config.yml reads
    # them apart from on_approval. None inherits the global tier (see the
    # RepoPolicy gate methods).
    model_config = {"frozen": True, "extra": "forbid"}

    plan_approval: GateValue | None = None
    implementation_approval: GateValue | None = None


class VerificationProfile(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}

    test_commands: tuple[str, ...] = ()
    lint_commands: tuple[str, ...] = ()
    typecheck_commands: tuple[str, ...] = ()


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
        return await resolve_extension_config("ship", repo=repo, model=cls)

    def plan_approval_gate(self, auto_dispatch: bool) -> GateValue:
        return self.gates.plan_approval or ("none" if auto_dispatch else "human")

    def implementation_approval_gate(self) -> GateValue:
        return self.gates.implementation_approval or "human"

    async def verification_block(self, *, profile: dict[str, Any], repo: str | None) -> str:
        # The agent-facing verification guidance: the profile's effective
        # commands plus this repo's sandbox env keys. ``profile`` is {} until the
        # repo profiler has run — the no-commands branch then carries the "don't
        # invent verification commands" guardrail.
        verification = profile.get("verification") or {}
        sections = [
            {"label": "Lint", "commands": verification.get("lint_commands", [])},
            {"label": "Typecheck", "commands": verification.get("typecheck_commands", [])},
            {"label": "Tests", "commands": verification.get("test_commands", [])},
        ]
        body = await render_prompt(
            "ship/verification_block.md",
            repo=repo,
            sections=sections,
            has_commands=any(section["commands"] for section in sections),
            sandbox_env_keys=sorted(self.sandbox.env),
        )
        return body.rstrip() + "\n"
