from pydantic import BaseModel, Field

from druks.agents import Agent
from druks.build.contracts import (
    CodeReviewOutput,
    ContractRevisionOutput,
    EvaluationOutput,
    ImplementationOutput,
    PlanOutput,
    RepoProfilerOutput,
    ReviewOutput,
    ScopeBriefOutput,
    TriageOutput,
)
from druks.build.models import WorkItem
from druks.build.schemas import WorkItemSummary
from druks.db import db_session
from druks.events import Event, FeedItem
from druks.extensions import Extension
from druks.workflows import Run, SubjectActivity, get_run_phase

_PHASE_META: dict[str, SubjectActivity] = {
    "provisioning_vm": SubjectActivity(label="Building sandbox VM…", kind="infra"),
    "agent_running": SubjectActivity(label="Working…", kind="agent"),
}


class Build(Extension):
    name = "build"
    subject_type = "work_item"
    # build's tables (projects, work_items, ...) are already unprefixed in core's
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
        scoper_candidate_statuses: tuple[str, ...] = Field(
            default=("Needs Refinement",),
            title="Scoper candidate statuses",
            description="Ticket statuses that trigger the scoper to write a brief.",
        )
        scoper_scoped_label: str = Field(
            default="druks-scoped",
            title="Scoped label",
            description="Label the scoper sets once briefed; remove it to force a re-scope.",
        )
        scoper_post_refinement_status: str = Field(
            default="Backlog",
            title="Linear post-refinement status",
            description="Status the scoper moves a briefed Linear ticket to; empty leaves it put.",
        )
        jira_scoper_post_refinement_status: str = Field(
            default="Open",
            title="Jira post-refinement status",
            description="Status the scoper moves a briefed Jira ticket to; empty leaves it put.",
        )

    @classmethod
    def post_refinement_status(cls, source: str) -> str:
        # The provider status name the scoper moves a briefed ticket to. Core
        # ticketing can't import this extension, so it takes the name as an argument.
        settings = cls.settings()
        if source == "jira":
            return settings.jira_scoper_post_refinement_status
        return settings.scoper_post_refinement_status

    # The build pipeline's agents — the extension owns them; any of its workflows run
    # them. The attribute name is each agent's id (its durable settings/timeline key).
    scope = Agent(
        description="ticket → brief",
        prompt="build/scope/scope_brief.md",
        contract=ScopeBriefOutput,
        model="codex",
    )
    generate_plan = Agent(
        description="brief → implementation plan",
        prompt="build/build_workflow/generate_plan.md",
        contract=PlanOutput,
        model="codex",
    )
    review_plan = Agent(
        description="critiques the plan before any work starts",
        prompt="build/build_workflow/review_plan.md",
        contract=ReviewOutput,
        model="claude",
    )
    revise_contract = Agent(
        description="revises the plan contract on feedback",
        prompt="build/build_workflow/revise_contract.md",
        contract=ContractRevisionOutput,
        model="codex",
    )
    implement = Agent(
        description="plan → diff, in a drukbox",
        prompt="build/build_workflow/implement.md",
        contract=ImplementationOutput,
        model="claude",
    )
    evaluate_implementation = Agent(
        description="adversarial review of the diff",
        prompt="build/build_workflow/evaluate_implementation.md",
        contract=EvaluationOutput,
        model="codex",
        effort="medium",
    )
    review_code = Agent(
        description="line-level code review on the PR",
        prompt="build/build_workflow/review_code.md",
        contract=CodeReviewOutput,
        model="claude",
    )
    triage_human_feedback = Agent(
        description="routes a human's PR feedback back into the workflow",
        prompt="build/build_workflow/triage_human_feedback.md",
        contract=TriageOutput,
        model="codex",
    )
    repo_profiler = Agent(
        description="reads a repo once and reports its stack, verification commands, and skills",
        prompt="build/profile/repo_profiler.md",
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
            source=run_kind or "build",
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
    def subject_summary(cls, subject_id: str) -> WorkItemSummary | None:
        item = WorkItem.get(int(subject_id))
        return WorkItemSummary.from_work_item(item) if item else None

    @classmethod
    def list_subjects(cls) -> list[WorkItemSummary]:
        # The active board: in-flight items only — handed-off ones live in History.
        # The 500 most-recent cover it; paginate if a board outgrows it.
        return [
            WorkItemSummary.from_work_item(item)
            for item in WorkItem.list_recent(limit=500)
            if not item.status
        ]

    @classmethod
    async def subject_activity(cls, subject_id: str) -> SubjectActivity | None:
        # Lazy import: the workflow module imports this extension at module top.
        from druks.build.workflows import Scope

        item = WorkItem.get(int(subject_id))
        if item:
            run = item.get_build_run()
            if not run:
                scope_runs = Run.list_for_subject("work_item", str(item.id), kind=Scope.kind)
                run = scope_runs[0] if scope_runs else None
            # Only while a run is actually RUNNING — a run parked on a gate isn't working.
            if run and run.is_running:
                phase = await get_run_phase(run.id)
                return _PHASE_META.get(phase or "")
        return
