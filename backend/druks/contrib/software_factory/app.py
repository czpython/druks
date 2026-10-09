import logging
from typing import Annotated, Literal

from pydantic import Field

from druks.agents import Agent
from druks.apps import App, AppSettings, Choices
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
from druks.contrib.software_factory.ticketing.base import Tracker
from druks.contrib.software_factory.ticketing.druks import DruksTracker
from druks.contrib.software_factory.ticketing.enums import TicketStatus
from druks.contrib.software_factory.ticketing.github import GitHub
from druks.contrib.software_factory.ticketing.jira import Jira
from druks.contrib.software_factory.ticketing.linear import Linear
from druks.core import services
from druks.doctor import CheckResult
from druks.services import ServiceNotConnectedError

from .services import GithubReviewer

logger = logging.getLogger(__name__)


async def check_tracker_identity() -> CheckResult:
    """Whether the selected tracker is connected. No tracker is a choice, and a
    selected tracker that is not connected is pending setup."""
    settings = await SoftwareFactory.settings()
    if settings.tracker == "none":
        return CheckResult(name="tracker", ok=True, detail="trackerless by choice")
    if settings.tracker == "druks":
        return CheckResult(name="tracker", ok=True, detail="this appliance")
    # The operator App is the GitHub tracker's identity; there is no second
    # credential to connect.
    service = {"linear": services.Linear, "jira": services.Jira, "github": services.Github}[
        settings.tracker
    ]
    if await service.is_connected():
        return CheckResult(name="tracker", ok=True, detail=f"{settings.tracker} connected")
    return CheckResult(
        name="tracker",
        ok=False,
        pending=True,
        detail=f"tracker is {settings.tracker} but it is not connected — "
        "connect it in Settings → Connections → Services.",
    )


async def list_tracker_status_choices() -> list[dict[str, str]]:
    """The statuses of the selected tracker. Empty when no connected tracker lists them."""
    tracker = await SoftwareFactory.get_tracker()
    if not tracker:
        return []
    async with tracker:
        try:
            return await tracker.list_status_choices()
        except tracker.known_exceptions:
            logger.warning("Could not list the tracker statuses.", exc_info=True)
            return []


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
    # The core migration history created these tables without a prefix.
    prefix_tables = False
    icon = "factory"
    description = (
        "Turns a ticket into a pull request — it plans the change, builds it, and "
        "gates on you before shipping. Reviews a pull request when asked."
    )

    class Settings(AppSettings):
        tracker: Literal["none", "linear", "jira", "druks", "github"] = Field(
            default="linear",
            title="Tracker",
            description="Which ticket tracker this installation uses.",
        )
        trigger_status: Annotated[str, Choices(list_tracker_status_choices)] = Field(
            default="Ready for Agent",
            title="Trigger status",
            description="A ticket entering this status opens a build.",
            json_schema_extra={
                "section": "Statuses",
                "visible_when": {"tracker": ["linear", "jira", "github"]},
            },
        )
        in_progress_status: Annotated[str, Choices(list_tracker_status_choices)] = Field(
            default="In Progress",
            title="In progress status",
            description="The status of a ticket while a build works on it.",
            json_schema_extra={
                "section": "Statuses",
                "visible_when": {"tracker": ["linear", "jira", "github"]},
            },
        )
        in_review_status: Annotated[str, Choices(list_tracker_status_choices)] = Field(
            default="In Review",
            title="In review status",
            description="The status of a ticket while its build waits for review. "
            "Empty leaves the ticket where it is.",
            json_schema_extra={
                "section": "Statuses",
                "visible_when": {"tracker": ["linear", "jira", "github"]},
            },
        )
        done_status: Annotated[str, Choices(list_tracker_status_choices)] = Field(
            default="Done",
            title="Done status",
            description="The status of a ticket after its pull request merges.",
            json_schema_extra={
                "section": "Statuses",
                "visible_when": {"tracker": ["linear", "jira", "github"]},
            },
        )
        resting_status: Annotated[str, Choices(list_tracker_status_choices)] = Field(
            default="Backlog",
            title="Resting status",
            description="The status of a ticket after druks stops work on it. "
            "Empty leaves the ticket where it is.",
            json_schema_extra={
                "section": "Statuses",
                "visible_when": {"tracker": ["linear", "jira", "github"]},
            },
        )

    checks = [check_tracker_identity, check_review_identity]

    @classmethod
    async def get_tracker(cls, source: str | None = None) -> Tracker | None:
        """The selected tracker, or None without one. A ``source`` returns the tracker
        only when that source is the selected tracker."""
        settings = await cls.settings()
        if source and source != settings.tracker:
            return
        if settings.tracker == "druks":
            return DruksTracker()
        status_names = {
            TicketStatus.TRIGGER: settings.trigger_status,
            TicketStatus.IN_PROGRESS: settings.in_progress_status,
            TicketStatus.IN_REVIEW: settings.in_review_status,
            TicketStatus.DONE: settings.done_status,
            TicketStatus.BACKLOG: settings.resting_status,
        }
        try:
            if settings.tracker == "linear":
                row = await services.Linear.get()
                return Linear(api_key=row.secrets["api_key"], status_names=status_names)
            if settings.tracker == "jira":
                row = await services.Jira.get()
                return Jira(
                    base_url=row.identity["base_url"],
                    email=row.identity["email"],
                    api_token=row.secrets["api_token"],
                    status_names=status_names,
                )
            if settings.tracker == "github":
                # Presence check only: the client resolves App credentials per
                # repository installation, not from this row.
                await services.Github.get()
                return GitHub(status_names=status_names)
        except ServiceNotConnectedError:
            return

    # Any workflow of the app can run these agents. An agent id is ``software_factory.<name>``.
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
