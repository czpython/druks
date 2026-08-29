from datetime import datetime
from typing import Any, Literal

from pydantic import AliasPath, BaseModel, ConfigDict, Field, computed_field

from druks.schemas import BaseResponse
from druks.workflows import SubjectSummary

# GitHub's verdict on a PR, verbatim.
PRResolution = Literal["merged", "closed"]


class ProjectRepoSummary(SubjectSummary):
    # The repo, wherever it is read: nested in its project, and as the header of its
    # own board. Where its profiling run stands is the platform's to say — the repo
    # is a subject, so that answer is its ``SubjectStatus``.
    full_name: str
    purpose: str | None = None
    # The profiler's findings as stored; {} until it has run.
    profile: dict[str, Any] = Field(default_factory=dict, validation_alias="effective_profile")
    created_at: datetime


class ProjectSummary(BaseResponse):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    created_at: datetime
    updated_at: datetime
    repos: list[ProjectRepoSummary] = Field(default_factory=list)


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


class WorkItemSummary(SubjectSummary):
    # The work item's domain header — what only Software Factory knows. Status (where it is
    # in its lifecycle) and the timeline come from the platform's subject read-side,
    # which composes this with them; ``id`` is the platform subject key (str).
    source: Literal["linear", "github", "jira"]
    repo: str
    # Druks Project name (e.g. "Acme"), not the repo. Required —
    # every WorkItem is born into a project, intake refuses tickets
    # whose Linear project doesn't map to one.
    project_name: str = Field(validation_alias=AliasPath("project", "name"))
    title: str
    ticket_key: str
    pr_number: int | None = None
    branch: str | None = None
    # Unset while the item is druks's to work on; set, it belongs to History.
    resolution: PRResolution | None = None
    created_at: datetime
    updated_at: datetime
    # Carried for the links below, which is where the UI reads it.
    ticket_url: str | None = Field(default=None, exclude=True)

    @computed_field
    @property
    def links(self) -> Links:
        pr = f"https://github.com/{self.repo}/pull/{self.pr_number}" if self.pr_number else None
        return Links(repo=f"https://github.com/{self.repo}", pr=pr, ticket=self.ticket_url)


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
    # The stored verdict, verbatim — the FE words and colors it. History holds only
    # resolved PRs, so both it and the time it carries are always set.
    resolution: PRResolution
    created_at: datetime
    updated_at: datetime = Field(validation_alias="resolved_at")

    @computed_field
    @property
    def key(self) -> str:
        return f"code:{self.source_id}"


class WorkItemsHistoryResponse(BaseResponse):
    items: list[DashboardItem]
