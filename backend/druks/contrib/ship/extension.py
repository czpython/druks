from typing import Literal

from pydantic import Field

from druks.agents import Agent
from druks.contrib.ship.contracts import (
    ContractRevisionOutput,
    EvaluationOutput,
    ImplementationOutput,
    PlanOutput,
    RepoProfilerOutput,
    ReviewOutput,
    TriageOutput,
)
from druks.contrib.ship.ticketing.base import Tracker
from druks.contrib.ship.ticketing.jira import Jira
from druks.contrib.ship.ticketing.linear import Linear
from druks.core import services
from druks.db import StoredSubject
from druks.doctor import CheckResult
from druks.extensions import Extension, ExtensionSettings
from druks.services import ServiceNotConnectedError
from druks.workflows import SubjectActivity

# Only what the timeline can't already show. A running agent has an agent call
# to name it, so the phase that clears provisioning maps to nothing.
_PHASE_META: dict[str, SubjectActivity] = {
    "provisioning_vm": SubjectActivity(label="Building sandbox VM…", kind="infra"),
}


def check_tracker_identity() -> CheckResult:
    """Whether the selected tracker's identity is connected. Trackerless is a
    choice, not a fault; a selected-but-unconnected tracker is pending setup."""
    settings = Ship.settings()
    if settings.tracker == "none":
        return CheckResult(name="tracker", ok=True, detail="trackerless by choice")
    service = {"linear": services.Linear, "jira": services.Jira}[settings.tracker]
    if service.is_connected():
        return CheckResult(name="tracker", ok=True, detail=f"{settings.tracker} connected")
    return CheckResult(
        name="tracker",
        ok=False,
        pending=True,
        detail=f"tracker is {settings.tracker} but it is not connected — "
        "connect it in Settings → Services.",
    )


class Ship(Extension):
    name = "ship"
    # These tables (projects, work_items, ...) are already unprefixed in core's
    # migration history, so they must stay that way.
    prefix_tables = False
    icon = "ship"
    description = (
        "Each stage of the build pipeline runs as its own agent. An agent runs its own "
        "model, or inherits its harness default — the backend dispatches the harness "
        "from the model you pick."
    )
    navigation = [("/ship", "active"), ("/ship/history", "history"), ("/ship/projects", "projects")]

    class Settings(ExtensionSettings):
        tracker: Literal["none", "linear", "jira"] = Field(
            default="linear",
            title="Tracker",
            description="Which ticket tracker this installation uses.",
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
            return ""

    checks = [check_tracker_identity]

    @classmethod
    def get_tracker(cls, source: str | None = None) -> Tracker | None:
        """The selected tracker, once its service identity is connected; None when
        the installation runs trackerless or the identity is missing. Pass a
        ``source`` to get it only when that source is the selected one — a
        work item syncs only to the tracker that owns it."""
        settings = cls.settings()
        if source is not None and source != settings.tracker:
            return
        try:
            if settings.tracker == "linear":
                row = services.Linear.get()
                return Linear(
                    api_key=row.secrets["api_key"],
                    backlog_status=settings.linear_resting_status,
                    trigger_status=settings.trigger_status,
                )
            if settings.tracker == "jira":
                row = services.Jira.get()
                return Jira(
                    base_url=row.identity["base_url"],
                    email=row.identity["email"],
                    api_token=row.secrets["api_token"],
                    backlog_status=settings.jira_resting_status,
                    trigger_status=settings.trigger_status,
                )
        except ServiceNotConnectedError:
            return

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
        description="verification + code review of the diff, one verdict",
        prompt="ship/build/evaluate_implementation.md",
        contract=EvaluationOutput,
        model="codex",
        effort="medium",
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
    async def get_subject_activity(cls, subject: StoredSubject) -> SubjectActivity | None:
        phase = await subject.get_phase()
        return _PHASE_META.get(phase or "")
