from pydantic import BaseModel, Field

from druks.agents import Agent
from druks.contrib.ship.contracts import (
    CodeReviewOutput,
    ContractRevisionOutput,
    EvaluationOutput,
    ImplementationOutput,
    PlanOutput,
    RepoProfilerOutput,
    ReviewOutput,
    TriageOutput,
)
from druks.contrib.ship.models import WorkItem
from druks.contrib.ship.schemas import WorkItemSummary
from druks.durable.models import Run
from druks.extensions import Extension
from druks.workflows import Subject, SubjectActivity

# Only what the timeline can't already show. A running agent has an agent call
# to name it, so the phase that clears provisioning maps to nothing.
_PHASE_META: dict[str, SubjectActivity] = {
    "provisioning_vm": SubjectActivity(label="Building sandbox VM…", kind="infra"),
}


class Ship(Extension):
    name = "ship"
    subject_type = WorkItem.subject_type
    # These tables (projects, work_items, ...) are already unprefixed in core's
    # migration history, so they must stay that way.
    prefix_tables = False
    icon = "hammer"
    description = (
        "Each stage of the build pipeline runs as its own agent. An agent runs its own "
        "model, or inherits its harness default — the backend dispatches the harness "
        "from the model you pick."
    )

    class Settings(BaseModel):
        # The tracker status names that drive build's funnel. They're operator
        # knobs — the names an operator's Linear/Jira workflow actually uses — so
        # they live here, not on core Settings.
        linear_trigger_status: str = Field(
            default="Ready for Agent",
            title="Linear trigger status",
            description="A Linear ticket entering this status opens a build.",
        )
        jira_trigger_status: str = Field(
            default="",
            title="Jira trigger status",
            description="A Jira ticket entering this status opens a build; empty disables Jira.",
        )
        linear_resting_status: str = Field(
            default="Backlog",
            title="Linear resting status",
            description=(
                "Status druks returns a ticket to when it stops working on it; empty leaves it put."
            ),
        )
        jira_resting_status: str = Field(
            default="Open",
            title="Jira resting status",
            description=(
                "Status druks returns a ticket to when it stops working on it; empty leaves it put."
            ),
        )

    @classmethod
    def resting_status(cls, source: str) -> str:
        # Core ticketing can't import this extension, so it takes the name as an argument.
        settings = cls.settings()
        if source == "jira":
            return settings.jira_resting_status
        return settings.linear_resting_status

    # The build pipeline's agents — the extension owns them; any of its workflows run
    # them. The attribute name is each agent's id (its durable settings/timeline key).
    generate_plan = Agent(
        description="ticket → implementation plan",
        prompt="ship/build/generate_plan.md",
        contract=PlanOutput,
        model="codex",
    )
    review_plan = Agent(
        description="critiques the plan before any work starts",
        prompt="ship/build/review_plan.md",
        contract=ReviewOutput,
        model="claude",
    )
    revise_contract = Agent(
        description="revises the plan contract on feedback",
        prompt="ship/build/revise_contract.md",
        contract=ContractRevisionOutput,
        model="codex",
    )
    implement = Agent(
        description="plan → diff, in a drukbox",
        prompt="ship/build/implement.md",
        contract=ImplementationOutput,
        model="claude",
    )
    evaluate_implementation = Agent(
        description="adversarial review of the diff",
        prompt="ship/build/evaluate_implementation.md",
        contract=EvaluationOutput,
        model="codex",
        effort="medium",
    )
    review_code = Agent(
        description="line-level code review on the PR",
        prompt="ship/build/review_code.md",
        contract=CodeReviewOutput,
        model="claude",
    )
    triage_human_feedback = Agent(
        description="routes a human's PR feedback back into the workflow",
        prompt="ship/build/triage_human_feedback.md",
        contract=TriageOutput,
        model="codex",
    )
    repo_profiler = Agent(
        description="reads a repo once and reports its stack, verification commands, and skills",
        prompt="ship/profile/repo_profiler.md",
        contract=RepoProfilerOutput,
        model="codex",
    )

    @classmethod
    def get_subject_summary(cls, subject: Subject) -> WorkItemSummary | None:
        item = WorkItem.get_for_subject(subject)
        if item:
            return WorkItemSummary.from_work_item(item)
        return

    @classmethod
    def list_subjects(cls) -> list[WorkItemSummary]:
        states = Run.subject_states(cls.subject_type)
        items = WorkItem.list_recent(limit=500)
        return [
            WorkItemSummary.from_work_item(item)
            for item in items
            if str(item.id) in states and item.pr_resolved_at is None
        ]

    @classmethod
    async def get_subject_activity(cls, subject: Subject) -> SubjectActivity | None:
        phase = await subject.get_phase()
        return _PHASE_META.get(phase or "")
