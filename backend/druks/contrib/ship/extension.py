from pydantic import BaseModel, Field, SecretStr

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
from druks.contrib.ship.ticketing.base import Tracker
from druks.contrib.ship.ticketing.checks import check_jira, check_linear
from druks.contrib.ship.ticketing.jira import Jira
from druks.contrib.ship.ticketing.linear import Linear
from druks.db import StoredSubject
from druks.extensions import Extension
from druks.workflows import SubjectActivity

# Only what the timeline can't already show. A running agent has an agent call
# to name it, so the phase that clears provisioning maps to nothing.
_PHASE_META: dict[str, SubjectActivity] = {
    "provisioning_vm": SubjectActivity(label="Building sandbox VM…", kind="infra"),
}


def secret_value(secret: SecretStr | None) -> str:
    return secret.get_secret_value() if secret else ""


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
    checks = [check_linear, check_jira]

    class Settings(BaseModel):
        linear_api_key: SecretStr | None = Field(
            default=None,
            title="Linear API key",
            description="API key used to read and update Linear tickets.",
        )
        linear_webhook_secret: SecretStr | None = Field(
            default=None,
            title="Linear webhook secret",
            description="Secret used to authenticate Linear webhook deliveries.",
        )
        jira_base_url: str = Field(
            default="",
            title="Jira base URL",
            description="Base URL of the Jira Cloud site.",
        )
        jira_email: str = Field(
            default="",
            title="Jira email",
            description="Email address used to authenticate with Jira Cloud.",
        )
        jira_api_token: SecretStr | None = Field(
            default=None,
            title="Jira API token",
            description="API token used to read and update Jira tickets.",
        )
        jira_webhook_secret: SecretStr | None = Field(
            default=None,
            title="Jira webhook secret",
            description="Secret used to authenticate Jira webhook deliveries.",
        )
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
    def tracker(cls, source: str) -> Tracker | None:
        """The configured tracker behind ``source``, or None when that source has
        no tracker (github) or its credentials aren't set yet."""
        settings = cls.settings()
        if source == "linear" and secret_value(settings.linear_api_key):
            return Linear(
                api_key=secret_value(settings.linear_api_key),
                ready_for_agent_status=settings.linear_resting_status,
            )
        if (
            source == "jira"
            and settings.jira_base_url
            and settings.jira_email
            and secret_value(settings.jira_api_token)
        ):
            return Jira(
                base_url=settings.jira_base_url,
                email=settings.jira_email,
                api_token=secret_value(settings.jira_api_token),
                ready_for_agent_status=settings.jira_resting_status,
            )
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
    async def get_subject_activity(cls, subject: StoredSubject) -> SubjectActivity | None:
        phase = await subject.get_phase()
        return _PHASE_META.get(phase or "")
