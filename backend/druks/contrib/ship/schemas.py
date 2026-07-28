from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import AliasPath, BaseModel, ConfigDict, Field, computed_field

from druks.schemas import BaseResponse
from druks.workflows import SubjectSummary

from .enums import HandoffStatus

if TYPE_CHECKING:
    from druks.contrib.ship.models import Project, ProjectRepo

ProfileState = Literal["unprofiled", "running", "ready", "failed"]


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
    def from_repo(cls, repo: "ProjectRepo") -> "ProjectRepoSummary":
        # "ready" outranks a later failed re-profile: a stored profile stays usable.
        status = repo.get_status()
        profile = repo.effective_profile()
        if status.is_running:
            profile_status, failure = "running", None
        elif profile:
            profile_status, failure = "ready", None
        elif status.is_failed:
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
    created_at: datetime
    updated_at: datetime
    repos: list[ProjectRepoSummary] = Field(default_factory=list)

    @classmethod
    def from_project(cls, project: "Project") -> "ProjectSummary":
        return cls(
            id=project.id,
            name=project.name,
            created_at=project.created_at,
            updated_at=project.updated_at,
            repos=[ProjectRepoSummary.from_repo(repo) for repo in project.repos],
        )


class ProjectsResponse(BaseResponse):
    projects: list[ProjectSummary]


class CreateProjectRequest(BaseModel):
    name: str


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

    @classmethod
    def for_work_item(
        cls, *, repo: str | None, pr_number: int | None, ticket_url: str | None
    ) -> "Links":
        pr = f"https://github.com/{repo}/pull/{pr_number}" if pr_number else None
        return cls(repo=f"https://github.com/{repo}", pr=pr, ticket=ticket_url)


class WorkItemSummary(SubjectSummary):
    # The work item's domain header — what only Ship knows. Status (where it is
    # in its lifecycle) and the timeline come from the platform's subject read-side,
    # which composes this with them; ``id`` is the platform subject key (str).
    source: Literal["linear", "github", "jira"]
    repo: str
    # Druks Project name (e.g. "Hey Fella"), not the repo. Required —
    # every WorkItem is born into a project, intake refuses tickets
    # whose Linear project doesn't map to one.
    project_name: str = Field(validation_alias=AliasPath("project", "name"))
    title: str
    ticket_key: str
    ticket_url: str | None = None
    pr_number: int | None = None
    branch: str | None = None
    created_at: datetime
    updated_at: datetime

    @computed_field
    @property
    def links(self) -> Links:
        return Links.for_work_item(
            repo=self.repo, pr_number=self.pr_number, ticket_url=self.ticket_url
        )


class DashboardItem(BaseResponse):
    model_config = ConfigDict(from_attributes=True)

    source_id: int | str = Field(validation_alias="id")
    ticket_key: str
    title: str
    repo: str | None = None
    pr_number: int | None = None
    # Druks Project is required on WorkItem, so the dashboard always has a
    # curated project name to render.
    project_name: str | None = Field(default=None, validation_alias=AliasPath("project", "name"))
    # The stored handoff lane, verbatim — the FE words and colors it. History is
    # terminal-only, so it is always set.
    status: HandoffStatus
    created_at: datetime
    updated_at: datetime
    # Carried for the links below, never serialized on its own.
    ticket_url: str | None = Field(default=None, exclude=True)

    @computed_field
    @property
    def key(self) -> str:
        return f"code:{self.source_id}"

    @computed_field
    @property
    def links(self) -> Links:
        return Links.for_work_item(
            repo=self.repo, pr_number=self.pr_number, ticket_url=self.ticket_url
        )


class WorkItemsHistoryResponse(BaseResponse):
    items: list[DashboardItem]
