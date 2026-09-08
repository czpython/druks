from typing import Literal

import httpx
from pydantic import Field

from druks.agents import Agent
from druks.apps import App, AppSettings, Secret
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
from druks.contrib.software_factory.issues.enums import Status as IssuesStatus
from druks.contrib.software_factory.ticketing.base import Tracker
from druks.contrib.software_factory.ticketing.issues import IssuesTracker
from druks.contrib.software_factory.ticketing.jira import Jira
from druks.contrib.software_factory.ticketing.linear import Linear
from druks.core import services
from druks.db import StoredSubject
from druks.doctor import CheckResult
from druks.services import ServiceNotConnectedError
from druks.settings import load_settings
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
        return CheckResult(name="tracker", ok=True, detail="local issues board")
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
    """Set or unset, both healthy: an empty pair is comment mode by design. A
    half-configured pair fails the settings check, not this one."""
    settings = await SoftwareFactory.settings()
    if settings.review_app_id and settings.review_private_key:
        return CheckResult(
            name="review_identity", ok=True, detail="set — reviews approve as the distinct App"
        )
    return CheckResult(
        name="review_identity", ok=True, detail="unset — reviews post as operator comments"
    )


async def check_issues_mcp() -> CheckResult:
    """Whether this appliance's /mcp answers, so an issues build can fetch
    and comment. Linear and Jira do not need it."""
    if (await SoftwareFactory.settings()).tracker != "issues":
        return CheckResult(name="issues_mcp", ok=True, detail="not required")
    endpoint = load_settings().urls.endpoint.rstrip("/")
    if not endpoint:
        return CheckResult(
            name="issues_mcp",
            ok=False,
            pending=True,
            detail="urls.endpoint is unset — the sandbox needs it to reach /mcp.",
        )
    url = f"{endpoint}/mcp"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url)
    except httpx.RequestError as error:
        return CheckResult(
            name="issues_mcp",
            ok=False,
            detail=f"{url} is unreachable: {error}. The issues tracker tools need it.",
        )
    if response.status_code >= 500:
        return CheckResult(
            name="issues_mcp",
            ok=False,
            detail=f"{url} returned {response.status_code}. The issues tracker tools need it.",
        )
    return CheckResult(name="issues_mcp", ok=True, detail=url)


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
        tracker: Literal["none", "linear", "jira", "issues"] = Field(
            default="linear",
            title="Tracker",
            description="Which ticket tracker this installation uses.",
            json_schema_extra={
                "choice_details": {
                    "issues": {
                        "label": "druks",
                        "help": "Druks is this appliance — no credentials.",
                    },
                },
            },
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
        # The optional distinct review identity. An empty pair borrows the operator
        # client in comment mode. A complete pair posts verdict reviews as this App.
        # App-owned: a posting identity for one app is not a platform service identity.
        review_app_id: Secret = Field(
            title="Review App ID",
            description="GitHub App ID of the distinct review identity; empty posts as comments.",
            json_schema_extra={"section": "Review identity"},
        )
        review_private_key: Secret = Field(
            title="Review App private key",
            description="PEM private key of the review App, pasted as issued.",
            json_schema_extra={"section": "Review identity", "multiline": True},
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

        def clean(self) -> dict[str, str]:
            problems: dict[str, str] = {}
            if self.review_app_id and not self.review_private_key:
                problems["review_private_key"] = "Required once the review App ID is set."
            if self.review_private_key and not self.review_app_id:
                problems["review_app_id"] = "Required once the review App private key is set."
            return problems

    checks = [check_tracker_identity, check_review_identity, check_issues_mcp]

    @classmethod
    async def get_tracker(cls, source: str | None = None) -> Tracker | None:
        """The selected tracker. Linear and Jira need a connected service identity.
        The local issues board does not. None when the installation runs
        trackerless or the identity is missing. Pass a ``source`` to get it only
        when that source is the selected one — a work item syncs only to the
        tracker that owns it."""
        settings = await cls.settings()
        if source and source != settings.tracker:
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

    @classmethod
    async def get_subject_activity(cls, subject: StoredSubject) -> SubjectActivity | None:
        phase = await subject.get_phase()
        return _PHASE_META.get(phase or "")
