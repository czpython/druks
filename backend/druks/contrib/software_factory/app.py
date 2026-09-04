from typing import Literal

from pydantic import Field

from druks.agents import Agent
from druks.apps import App, AppSettings
from druks.contrib.issues.enums import Status as IssuesStatus
from druks.contrib.issues.tracker import IssuesTracker
from druks.contrib.software_factory.contracts import (
    ContractRevisionOutput,
    EvaluationOutput,
    ImplementationOutput,
    PlanOutput,
    RepoProfilerOutput,
    ReviewOutput,
    TriageOutput,
)
from druks.contrib.software_factory.ticketing.base import Tracker
from druks.contrib.software_factory.ticketing.jira import Jira
from druks.contrib.software_factory.ticketing.linear import Linear
from druks.core import services
from druks.db import StoredSubject
from druks.doctor import CheckResult
from druks.services import ServiceNotConnectedError
from druks.workflows import SubjectActivity

# Only what the timeline can't already show. A running agent has an agent call
# to name it, so the phase that clears provisioning maps to nothing.
_PHASE_META: dict[str, SubjectActivity] = {
    "provisioning_vm": SubjectActivity(label="Provisioning sandbox VM…", kind="infra"),
    "sandbox_building": SubjectActivity(label="Building sandbox…", kind="infra"),
}


async def check_tracker_identity() -> CheckResult:
    """Whether the selected tracker's identity is connected. Trackerless is a
    choice, not a fault; a selected-but-unconnected tracker is pending setup."""
    settings = await SoftwareFactory.settings()
    if settings.tracker == "none":
        return CheckResult(name="tracker", ok=True, detail="trackerless by choice")
    if settings.tracker == "issues":
        return CheckResult(name="tracker", ok=True, detail="local issues app")
    service = {"linear": services.Linear, "jira": services.Jira}[settings.tracker]
    if await service.is_connected():
        return CheckResult(name="tracker", ok=True, detail=f"{settings.tracker} connected")
    return CheckResult(
        name="tracker",
        ok=False,
        pending=True,
        detail=f"tracker is {settings.tracker} but it is not connected — "
        "connect it in Settings → Services.",
    )


class SoftwareFactory(App):
    name = "software_factory"
    # These tables (projects, work_items, ...) are already unprefixed in core's
    # migration history, so they must stay that way.
    prefix_tables = False
    icon = "factory"
    description = (
        "Turns a ticket into a pull request — it plans the change, builds it, and "
        "gates on you before shipping."
    )

    class Settings(AppSettings):
        tracker: Literal["none", "linear", "jira", "issues"] = Field(
            default="linear",
            title="Tracker",
            description=(
                "Which ticket tracker this installation uses. "
                "Druks is this appliance — no credentials."
            ),
            json_schema_extra={"choice_labels": {"issues": "druks"}},
        )
        # The tracker status names that drive build's funnel. They're operator
        # knobs — the names an operator's Linear/Jira workflow actually uses — so
        # they live here, not on core Settings.
        linear_trigger_status: str = Field(
            default="Ready for Agent",
            title="Linear trigger status",
            description="A Linear ticket entering this status opens a build.",
            json_schema_extra={"section": "Linear", "visible_when": {"tracker": "linear"}},
        )
        linear_resting_status: str = Field(
            default="Backlog",
            title="Linear resting status",
            description=(
                "Status druks returns a ticket to when it stops working on it; empty leaves it put."
            ),
            json_schema_extra={"section": "Linear", "visible_when": {"tracker": "linear"}},
        )
        jira_trigger_status: str = Field(
            default="Ready for Agent",
            title="Jira trigger status",
            description="A Jira ticket entering this status opens a build.",
            json_schema_extra={"section": "Jira", "visible_when": {"tracker": "jira"}},
        )
        jira_resting_status: str = Field(
            default="Open",
            title="Jira resting status",
            description=(
                "Status druks returns a ticket to when it stops working on it; empty leaves it put."
            ),
            json_schema_extra={"section": "Jira", "visible_when": {"tracker": "jira"}},
        )

        @property
        def trigger_status(self) -> str:
            """The status that opens a build, on the tracker this installation uses."""
            if self.tracker == "linear":
                return self.linear_trigger_status
            if self.tracker == "jira":
                return self.jira_trigger_status
            if self.tracker == "issues":
                return IssuesStatus.READY_FOR_AGENT.label
            return ""

    checks = [check_tracker_identity]

    @classmethod
    async def get_tracker(cls, source: str | None = None) -> Tracker | None:
        """The selected tracker, once its service identity is connected; None when
        the installation runs trackerless or the identity is missing. Issues has
        no identity to connect. Pass a ``source`` to get it only when that
        source is the selected one — a work item syncs only to the tracker
        that owns it."""
        settings = await cls.settings()
        if source is not None and source != settings.tracker:
            return
        if settings.tracker == "issues":
            return IssuesTracker()
        try:
            if settings.tracker == "linear":
                row = await services.Linear.get()
                return Linear(
                    api_key=row.secrets["api_key"],
                    backlog_status=settings.linear_resting_status,
                    trigger_status=settings.trigger_status,
                )
            if settings.tracker == "jira":
                row = await services.Jira.get()
                return Jira(
                    base_url=row.identity["base_url"],
                    email=row.identity["email"],
                    api_token=row.secrets["api_token"],
                    backlog_status=settings.jira_resting_status,
                    trigger_status=settings.trigger_status,
                )
        except ServiceNotConnectedError:
            return

    # The build pipeline's agents — the app owns them; any of its workflows run
    # them. The attribute name is each agent's id (its durable settings/timeline key).
    generate_plan = Agent(
        description="ticket → implementation plan",
        prompt="software_factory/build/generate_plan.md",
        contract=PlanOutput,
    )
    review_plan = Agent(
        description="critiques the plan before any work starts",
        prompt="software_factory/build/review_plan.md",
        contract=ReviewOutput,
    )
    revise_contract = Agent(
        description="revises the plan contract on feedback",
        prompt="software_factory/build/revise_contract.md",
        contract=ContractRevisionOutput,
    )
    implement = Agent(
        description="plan → diff, in a drukbox",
        prompt="software_factory/build/implement.md",
        contract=ImplementationOutput,
    )
    evaluate_implementation = Agent(
        description="verification + code review of the diff, one verdict",
        prompt="software_factory/build/evaluate_implementation.md",
        contract=EvaluationOutput,
    )
    triage_human_feedback = Agent(
        description="routes a human's PR feedback back into the workflow",
        prompt="software_factory/build/triage_human_feedback.md",
        contract=TriageOutput,
    )
    repo_profiler = Agent(
        description="reads a repo once and reports its stack, verification commands, and skills",
        prompt="software_factory/profile/repo_profiler.md",
        contract=RepoProfilerOutput,
    )

    @classmethod
    async def get_subject_activity(cls, subject: StoredSubject) -> SubjectActivity | None:
        phase = await subject.get_phase()
        return _PHASE_META.get(phase or "")
