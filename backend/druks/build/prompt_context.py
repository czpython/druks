from dataclasses import dataclass

from druks.build.journal import BuildJournal
from druks.build.models import ProjectRepo


@dataclass(frozen=True)
class BuildPromptContext:
    """What build's prompt templates render against — the run's identity facts
    plus its journal, assembled per agent call by ``BuildWorkflow.get_prompt_context``.
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
    related_repos: list[ProjectRepo]
    journal: BuildJournal
