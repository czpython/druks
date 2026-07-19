from typing import Literal

from druks.agents import AgentOutput


class RelatedRepoOutput(AgentOutput):
    full_name: str
    purpose: str


class ScopeBriefOutput(AgentOutput):
    status: Literal["ready", "needs_answers"]
    problem: str
    scope: str
    acceptance_criteria: list[str]
    stack_hints: list[str]
    related_repos: list[RelatedRepoOutput]
    out_of_scope: list[str]
    # Operator-stated contracts the implementer must honor verbatim — the
    # uncompressed escape hatch the compressed brief loses by design.
    decisions: list[str]
    open_questions: list[str]
