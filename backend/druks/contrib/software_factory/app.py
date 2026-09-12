from typing import Literal

from pydantic import Field

from druks.agents import Agent
from druks.apps import App, AppSettings
from druks.contrib.software_factory.contracts import (
    ContractRevisionOutput,
    EvaluationOutput,
    ImplementationOutput,
    PlanOutput,
    RepoProfilerOutput,
    ReviewOutput,
    ReviewReport,
    TriageOutput,
)
from druks.contrib.software_factory.enums import Status
from druks.contrib.software_factory.ticketing.base import Tracker
from druks.contrib.software_factory.ticketing.druks import DruksTracker
from druks.contrib.software_factory.ticketing.jira import Jira
from druks.contrib.software_factory.ticketing.linear import Linear
from druks.core import services
from druks.doctor import CheckResult
from druks.services import ServiceNotConnectedError

from .services import GithubReviewer


async def check_tracker_identity() -> CheckResult:
    """Whether the selected tracker's identity is connected. Trackerless is a
    choice, not a fault; a selected-but-unconnected tracker is pending setup."""
    settings = await SoftwareFactory.settings()
    if settings.tracker == "none":
        return CheckResult(name="tracker", ok=True, detail="trackerless by choice")
    if settings.tracker == "druks":
        return CheckResult(name="tracker", ok=True, detail="this appliance")
    service = {"linear": services.Linear, "jira": services.Jira}[settings.tracker]
    if await service.is_connected():
        return CheckResult(name="tracker", ok=True, detail=f"{settings.tracker} connected")
    return CheckResult(
        name="tracker",
        ok=False,
        pending=True,
        detail=f"tracker is {settings.tracker} but it is not connected — "
        "connect it in Settings → Connections → Services.",
    )


async def check_review_identity() -> CheckResult:
    """Connected or not, both healthy: no reviewer is comment mode by design."""
    if await GithubReviewer.is_connected():
        return CheckResult(
            name="review_identity",
            ok=True,
            detail="connected — reviews approve as the reviewer App",
        )
    return CheckResult(
        name="review_identity", ok=True, detail="unset — reviews publish as operator comments"
    )


class SoftwareFactory(App):
    name = "software_factory"
    # These tables (projects, work_items, ...) are already unprefixed in core's
    # migration history, so they must stay that way.
    prefix_tables = False
    icon = "factory"
    description = (
        "Turns a ticket into a pull request — it plans the change, builds it, and "
        "gates on you before shipping. Reviews a pull request when asked."
    )

    class Settings(AppSettings):
        tracker: Literal["none", "linear", "jira", "druks"] = Field(
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
            if self.tracker == "druks":
                return Status.READY_FOR_AGENT.label
            return ""

    checks = [check_tracker_identity, check_review_identity]

    @classmethod
    async def get_tracker(cls, source: str | None = None) -> Tracker | None:
        """The selected tracker, or None when this installation runs without one.
        Linear and Jira need a connected service identity. The board on this
        appliance needs none. Pass a ``source`` to get the tracker only when that
        source is the selected one. A work item syncs to the tracker that owns it."""
        settings = await cls.settings()
        if source and source != settings.tracker:
            return
        if settings.tracker == "druks":
            return DruksTracker()
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

    # The app's agents — any of its workflows run them. The app name and the
    # attribute name form each agent's id (``software_factory.implement``), its
    # durable settings and timeline key.
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
    review_pull_request = Agent(
        description="reads a pull request and writes the review",
        prompt="software_factory/review/review_pull_request.md",
        contract=ReviewReport,
    )
