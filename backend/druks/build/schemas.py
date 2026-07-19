from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from druks.build.models import Project, ProjectRepo, WorkItem
from druks.durable.enums import RunState
from druks.schemas import BaseResponse
from druks.workflows import SubjectSummary

from .enums import HandoffStatus, Outcome

ProfileState = Literal["unprofiled", "running", "ready", "failed"]


def _outcome_from_status(status: str) -> tuple[str, Outcome]:
    # The stored handoff lane → (label, outcome). History is terminal-only, so this
    # is the whole derivation: a handed-off item's outcome is just its status.
    if status == HandoffStatus.SHIPPED:
        return "Complete", Outcome.FINISHED
    if status == HandoffStatus.CANCELLED:
        return "Cancelled", Outcome.CANCELLED
    return "Scoped", Outcome.SCOPED


class ProjectRepoSummary(BaseResponse):
    id: int
    full_name: str
    purpose: str | None = None
    # The stored effective profile as-is; {} until the repo profiler has run.
    profile: dict[str, Any] = Field(default_factory=dict)
    profile_status: ProfileState
    profiler_run_failure: str | None = None
    created_at: datetime

    @classmethod
    def from_repo(cls, repo: ProjectRepo) -> "ProjectRepoSummary":
        # "ready" outranks a later failed re-profile: a stored profile stays usable.
        status = repo.profiler_status()
        profile = repo.effective_profile()
        if status.state == RunState.RUNNING:
            profile_status, failure = "running", None
        elif profile:
            profile_status, failure = "ready", None
        elif status.state == RunState.FAILED:
            profile_status, failure = "failed", status.failure
        else:
            profile_status, failure = "unprofiled", None
        return cls(
            id=repo.id,
            full_name=repo.full_name,
            purpose=repo.purpose,
            profile=profile,
            profile_status=profile_status,
            profiler_run_failure=failure,
            created_at=repo.created_at,
        )


class ProjectSummary(BaseResponse):
    id: int
    name: str
    slug: str
    created_at: datetime
    updated_at: datetime
    repos: list[ProjectRepoSummary] = Field(default_factory=list)

    @classmethod
    def from_project(cls, project: Project) -> "ProjectSummary":
        return cls(
            id=project.id,
            name=project.name,
            slug=project.slug,
            created_at=project.created_at,
            updated_at=project.updated_at,
            repos=[ProjectRepoSummary.from_repo(repo) for repo in project.repos],
        )


class ProjectsResponse(BaseResponse):
    projects: list[ProjectSummary]


class CreateProjectRequest(BaseModel):
    name: str
    slug: str | None = None


class AddProjectRepoRequest(BaseModel):
    full_name: str = Field(alias="fullName")
    purpose: str | None = None


class GitHubRepoSummary(BaseResponse):
    full_name: str
    description: str | None = None


class GitHubReposResponse(BaseResponse):
    repos: list[GitHubRepoSummary]


class Links(BaseResponse):
    repo: str
    pr: str | None = None
    ticket: str | None = None


class WorkItemSummary(SubjectSummary):
    # The work item's domain header — what only build knows. Status (where it is
    # in its lifecycle) and the timeline come from the platform's subject read-side,
    # which composes this with them; ``id`` is the platform subject key (str).
    source: Literal["linear", "github", "jira"]
    repo: str
    # Druks Project name (e.g. "Hey Fella"), not the repo. Required —
    # every WorkItem is born into a project, intake refuses tickets
    # whose Linear project doesn't map to one.
    project_name: str
    title: str
    remote_key: str | None = None
    remote_url: str | None = None
    pr_number: int | None = None
    branch: str | None = None
    created_at: datetime
    updated_at: datetime
    links: Links

    @classmethod
    def from_work_item(cls, item: WorkItem) -> "WorkItemSummary":
        repo_url = f"https://github.com/{item.repo}"
        pr_url = f"https://github.com/{item.repo}/pull/{item.pr_number}" if item.pr_number else None
        return cls(
            id=str(item.id),
            source=item.source,  # type: ignore[arg-type]
            repo=item.repo,
            project_name=item.project.name,
            title=item.title,
            remote_key=item.remote_key,
            remote_url=item.remote_url,
            pr_number=item.pr_number,
            branch=item.branch,
            created_at=item.created_at,
            updated_at=item.updated_at,
            links=Links(repo=repo_url, pr=pr_url, ticket=item.remote_url),
        )


class DashboardItem(BaseResponse):
    key: str
    source_id: int | str
    ticket_ref: str | None = None
    title: str
    repo: str | None = None
    pr_number: int | None = None
    project_name: str | None = None
    # The terminal human label ("Complete", "Cancelled", "Scoped") — History rows
    # show this alongside the ``outcome`` glyph.
    status: str
    outcome: Outcome | None = None
    created_at: datetime
    updated_at: datetime
    links: Links

    @classmethod
    def from_work_item(cls, item: WorkItem) -> "DashboardItem":
        assert item.status is not None  # History is terminal-only: status is always set.
        repo_url = f"https://github.com/{item.repo}"
        pr_url = f"https://github.com/{item.repo}/pull/{item.pr_number}" if item.pr_number else None
        label, outcome = _outcome_from_status(item.status)
        return cls(
            key=f"code:{item.id}",
            source_id=item.id,
            ticket_ref=item.remote_key,
            title=item.title,
            repo=item.repo,
            pr_number=item.pr_number,
            # Druks Project is now required on WorkItem, so the dashboard
            # always has a curated project name to render.
            project_name=item.project.name,
            status=label,
            outcome=outcome,
            created_at=item.created_at,
            updated_at=item.updated_at,
            links=Links(repo=repo_url, pr=pr_url, ticket=item.remote_url),
        )


class WorkItemsHistoryResponse(BaseResponse):
    items: list[DashboardItem]
