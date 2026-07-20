from dataclasses import dataclass

from druks.build.contracts import (
    AcceptanceCriterionOutput,
    EvaluationOutput,
    HumanFeedback,
    ReviewOutput,
)
from druks.build.models import ProjectRepo


@dataclass(frozen=True)
class BuildPromptContext:
    """What build's prompt templates render against — a flat snapshot of the run's
    state, assembled once per agent call by ``BuildWorkflow.get_prompt_context``.
    The templates read ``build.<field>`` and nothing else off the run, so this is
    the whole contract between the workflow and its prompts; the workflow itself
    stays free of template accessors.
    """

    # Target repo + ticket identity, flattened from the run input.
    repo: str | None
    branch: str | None
    pr_number: int | None
    ticket_ref: str | None
    source: str | None
    issue_number: int | None
    task_owner_name: str | None
    task_owner_email: str | None
    # Where the run stands in its plan / implement loops.
    plan_revision: int
    implementation_revision: int
    finalized_base_sha: str | None
    finalized_pr_sha: str | None
    # The current plan the downstream agents build on.
    current_plan: str
    acceptance_criteria: list[AcceptanceCriterionOutput]
    reviewer_requirements: list[ReviewOutput]
    implementation_reviews: list[EvaluationOutput]
    human_feedback: list[HumanFeedback]
    related_repos: list[ProjectRepo]
