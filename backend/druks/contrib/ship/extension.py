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
from druks.db import db_session
from druks.events import Event, FeedItem
from druks.extensions import Extension
from druks.workflows import SubjectActivity, get_subject_phase

_PHASE_META: dict[str, SubjectActivity] = {
    "provisioning_vm": SubjectActivity(label="Building sandbox VM…", kind="infra"),
    "agent_running": SubjectActivity(label="Working…", kind="agent"),
}


class Ship(Extension):
    name = "ship"
    subject = WorkItem
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
    _LABEL = {
        "run.running": "started",
        "run.finished": "finished",
        "run.failed": "failed",
        "run.cancelled": "cancelled",
        "run.pending_input": "waiting on you",
        "needs_answers": "needs answers",
    }

    @classmethod
    def format_event(cls, event: Event) -> FeedItem:
        wid = cls._work_item_id(event)
        # ``session.get`` rides the identity map, so a feed with several events on
        # the same work item costs one title lookup, not one per event.
        item = db_session().get(WorkItem, wid) if wid else None
        ticket_ref = (item.remote_key or "") if item else ""
        run_kind = event.payload.get("kind")
        if run_kind:
            # The feed shows the workflow's local name, not its namespaced durable kind.
            run_kind = run_kind.rsplit(".", 1)[-1]
        label = cls._LABEL.get(event.type, event.type)
        if event.type.startswith("run."):
            kind, summary = event.type, (f"{run_kind} {label}" if run_kind else label)
        else:
            kind, summary = f"milestone.{event.type}", label
        ref = ticket_ref or (f"work item {wid}" if wid else "")
        if ref:
            summary = f"{summary} — {ref}"
        return FeedItem(
            id=f"event:{event.id}",
            at=event.created_at,
            kind=kind,
            source=run_kind or "ship",
            summary=summary,
            link_path=f"/work-items/{wid}" if wid else None,
            meta={"ticketRef": ticket_ref} if ticket_ref else {},
        )

    @staticmethod
    def _work_item_id(event: Event) -> int | None:
        if event.subject_type == "work_item" and event.subject_id:
            return int(event.subject_id)
        return

    @classmethod
    def subject_summary(cls, subject: WorkItem) -> WorkItemSummary:
        return WorkItemSummary.from_work_item(subject)

    @classmethod
    def list_subjects(cls) -> list[WorkItemSummary]:
        # The active board: whatever hasn't handed off yet. The 500 most-recent
        # cover it; paginate if a board outgrows it.
        return [WorkItemSummary.from_work_item(item) for item in WorkItem.list_open(limit=500)]

    @classmethod
    async def subject_activity(cls, subject: WorkItem) -> SubjectActivity | None:
        phase = await get_subject_phase(subject.subject_type, str(subject.id))
        return _PHASE_META.get(phase or "")
