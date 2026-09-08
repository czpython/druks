from dataclasses import dataclass

from druks.contrib.software_factory.journal import BuildJournal
from druks.contrib.software_factory.models import ProjectRepo
from druks.skills.models import Skill

# How the prompt names the ticket's home. ``issues`` must not render as
# "Issues" — that reads as GitHub Issues and the agent fetches the wrong tool.
TRACKER_LABELS = {
    "linear": "Linear",
    "jira": "Jira",
    "github": "GitHub",
    "issues": "the Druks board",
}


@dataclass(frozen=True)
class BuildPromptContext:
    """What build's prompt templates render against — the run's identity facts
    plus its journal, assembled per agent call by ``Build.get_prompt_context``.
    The templates read ``build.<field>`` and nothing else off the run, so this is
    the whole contract between the workflow and its prompts; the workflow itself
    stays free of template accessors.
    """

    # Target repo + ticket identity, flattened from the run input.
    repo: str | None
    work_item_url: str
    branch: str | None
    pr_number: int | None
    ticket_ref: str | None
    source: str | None
    tracker_label: str
    issue_number: int | None
    task_owner_name: str | None
    task_owner_email: str | None
    related_repos: list[ProjectRepo]
    skills: list[Skill]
    review_code: bool
    # How the github MCP identity may post reviews: "approve" (distinct
    # review identity) or "comment" (the operator, barred from approving its
    # own pull requests).
    review_mode: str
    journal: BuildJournal
