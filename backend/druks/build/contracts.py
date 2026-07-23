from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel, Field, model_validator

from druks.agents import AgentOutput
from druks.build.enums import (
    EvaluationVerdict,
    HumanFeedbackAction,
    ReviewDecision,
)
from druks.workflows import Gate, Workflow

if TYPE_CHECKING:
    from druks.build.workflows import BuildWorkflow


# The PR webhook resumes approve/request_changes; revise_contract and cancel
# come from the operator's UI.
class ReviewWork(Gate):
    name = "review_work"
    action: Literal["approve", "request_changes", "revise_contract", "cancel"]
    reviewer: str | None = None
    body: str | None = None

    @classmethod
    async def on_wait(cls, workflow: Workflow) -> None:
        build = cast("BuildWorkflow", workflow)
        await build.set_pr_draft(draft=False)
        await build.request_assignee_review()


class RepoProfilerOutput(AgentOutput):
    languages: list[str]
    frameworks: list[str]
    package_managers: list[str]
    stack_summary: str
    test_commands: list[str]
    lint_commands: list[str]
    typecheck_commands: list[str]
    # Skills the profiler judges an implementer will need to build here — not
    # skills bundled in the repo.
    recommended_skills: list[str]

    def to_result(self) -> dict[str, Any]:
        # The stored profile shape — ProjectRepo.profile holds it as plain JSON,
        # so it stays a dict end to end.
        return {
            "languages": self.languages,
            "frameworks": self.frameworks,
            "package_managers": self.package_managers,
            "stack_summary": self.stack_summary,
            "verification": {
                "test_commands": self.test_commands,
                "lint_commands": self.lint_commands,
                "typecheck_commands": self.typecheck_commands,
            },
            "recommended_skills": self.recommended_skills,
        }


class QuestionOptionOutput(AgentOutput):
    # Identity and cardinality are contract-capped at the agent boundary so a
    # parked ask (and the gate view built from it) is bounded by construction —
    # an oversized answer fails parse loudly instead of being clipped later.
    id: str = Field(max_length=64)
    label: str = Field(max_length=256)


class QuestionOutput(AgentOutput):
    id: str = Field(max_length=64)
    prompt: str = Field(max_length=2048)
    options: list[QuestionOptionOutput] = Field(max_length=16)


class AcceptanceCriterionOutput(AgentOutput):
    id: str
    description: str
    verification: str


class PlanData(BaseModel):
    # The workflow's current plan, normalized from either producer — the
    # generate_plan agent (questions and all) or a contract revision (questions
    # resolved). It carries the agents' own output shapes; a re-plan reads the
    # questions and acceptance criteria straight off them.
    plan_markdown: str = ""
    questions: list[QuestionOutput] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterionOutput] = Field(default_factory=list)
    # Resolved by the planner; a revision carries None.
    assignee_github_login: str | None = None

    def get_answered(self, picks: dict[str, str]) -> list[dict[str, str]]:
        # Each question the operator answered, paired with its answer — what the
        # re-plan agent reads to resolve it. A pick matching an offered option maps
        # to that option's label; anything else is the operator's own words, kept
        # verbatim.
        pairs = []
        for question in self.questions:
            chosen = picks.get(question.id)
            if not chosen:
                continue
            label = next(
                (option.label for option in question.options if option.id == chosen), chosen
            )
            pairs.append({"question": question.prompt, "answer": label})
        return pairs


class PlanOutput(AgentOutput):
    plan_markdown: str
    acceptance_criteria: list[AcceptanceCriterionOutput]
    questions: list[QuestionOutput] = Field(max_length=8)
    # Required but nullable: the planner always reports the field, null when it
    # resolved no assignee login convincingly.
    assignee_github_login: str | None

    def get_artifact(self) -> dict[str, str]:
        return {"kind": "markdown", "title": "Implementation plan", "content": self.plan_markdown}

    def to_result(self) -> PlanData:
        return PlanData(
            plan_markdown=self.plan_markdown,
            acceptance_criteria=self.acceptance_criteria,
            questions=self.questions,
            assignee_github_login=self.assignee_github_login,
        )


class ContractRevisionOutput(AgentOutput):
    plan_markdown: str
    acceptance_criteria: list[AcceptanceCriterionOutput]
    implementation_instructions: str

    def get_artifact(self) -> dict[str, str]:
        return {"kind": "markdown", "title": "Implementation plan", "content": self.plan_markdown}

    def to_result(self) -> PlanData:
        # A revision resolves the questions, so none carry over;
        # implementation_instructions ride the prompt, not the plan artifact.
        return PlanData(
            plan_markdown=self.plan_markdown,
            acceptance_criteria=self.acceptance_criteria,
            questions=[],
        )


class ReviewOutput(AgentOutput):
    # No get_artifact: the plan must stay the parked ask's resolved document;
    # the fallback park sends the critique as ask context instead.
    decision: Literal[ReviewDecision.APPROVE, ReviewDecision.REQUEST_CHANGES]
    body: str


class TriageOutput(AgentOutput):
    action: HumanFeedbackAction
    body: str
    question: str
    implementation_instructions: str


class AcceptanceEvidenceOutput(AgentOutput):
    id: str
    status: Literal["implemented", "partial", "not_implemented"]
    evidence: str


class CommandCheckOutput(AgentOutput):
    command: str
    status: Literal["pass", "fail", "not_run"]
    exit_code: int | None
    reason: str


class ImplementationOutput(AgentOutput):
    type: Literal["result"]
    # ``needs_clarification`` = the implementer found a contradiction in the binding
    # requirements and bailed; ``summary`` carries the reason. The workflow turns
    # that into a run-stopping failure.
    status: Literal["success", "needs_clarification"]
    # Nullable only for needs_clarification (bailed before delivering) — the strict
    # schema bans defaults, so optional is spelled required-but-nullable. On success
    # the validator below demands all five: a "success" without a pushed commit on a
    # PR is the fabrication this contract exists to reject.
    base_sha: str | None
    head_sha: str | None
    commit_sha: str | None
    # The branch pushed to and the PR delivered on — the one from the workflow context, or
    # the pair the implementer provisioned on the first pass.
    branch: str | None
    pr_number: int | None
    files_changed: list[str]
    acceptance_results: list[AcceptanceEvidenceOutput]
    checks: list[CommandCheckOutput]
    known_risks: list[str]
    summary: str
    workspace_path: str
    workspace_retention: str | None

    @model_validator(mode="after")
    def _success_means_delivered(self) -> "ImplementationOutput":
        if self.status != "success":
            return self
        undelivered = [
            name
            for name in ("base_sha", "head_sha", "commit_sha", "branch", "pr_number")
            if not getattr(self, name)
        ]
        if undelivered:
            raise ValueError(
                f"status=success without {', '.join(undelivered)} — a delivery has a "
                "pushed commit on a PR; return needs_clarification if you could not deliver"
            )
        return self


class FindingOutput(AgentOutput):
    severity: Literal["high", "medium", "low"]
    summary: str
    evidence: str
    path: str | None
    line: int | None
    start_line: int | None


class EvalCheckOutput(AgentOutput):
    name: str
    status: Literal["pass", "fail", "not_run"]
    evidence: str


class AcceptanceResultOutput(AgentOutput):
    criterion_id: str
    status: Literal["pass", "fail", "not_run"]
    evidence: str


class EvaluationOutput(AgentOutput):
    verdict: EvaluationVerdict
    body: str
    findings: list[FindingOutput]
    checks: list[EvalCheckOutput]
    acceptance_results: list[AcceptanceResultOutput]


class CodeReviewOutput(AgentOutput):
    summary: str


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
