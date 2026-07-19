from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from druks.build.models import Project, ProjectRepo, WorkItem
from druks.durable.enums import RunState
from druks.durable.schemas import RunSummary, SubjectStatus, clip
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
    created_at: datetime
    updated_at: datetime
    repos: list[ProjectRepoSummary] = Field(default_factory=list)

    @classmethod
    def from_project(cls, project: Project) -> "ProjectSummary":
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
    def from_work_item(cls, item: WorkItem, *, ticket_clip: int | None = None) -> "Links":
        # The tracker URL is client-supplied and unbounded; budgeted (agent)
        # projections pass ticket_clip so a page holds its byte contract — the
        # dashboard keeps the whole URL for click-through.
        pr = f"https://github.com/{item.repo}/pull/{item.pr_number}" if item.pr_number else None
        ticket = item.remote_url
        if ticket_clip:
            ticket = clip(ticket, ticket_clip)
        return cls(repo=f"https://github.com/{item.repo}", pr=pr, ticket=ticket)


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
            links=Links.from_work_item(item),
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
            links=Links.from_work_item(item),
        )


class WorkItemsHistoryResponse(BaseResponse):
    items: list[DashboardItem]


# The agent surface clips titles so a page of rows holds its byte budget.
_TITLE_CLIP = 120
# SubjectStatus.failure is unbounded on the dashboard; the agent surface
# bounds it the same way the run summaries bound theirs.
_FAILURE_CLIP = 160
# The tracker URL is client-supplied; real ticket URLs sit well under this.
_URL_CLIP = 200


def _bounded_status(status: SubjectStatus) -> SubjectStatus:
    return status.model_copy(update={"failure": clip(status.failure, _FAILURE_CLIP)})


class AgentWorkItem(BaseResponse):
    # One board row for the agent surface: identity + links + the platform's
    # SubjectStatus facts, free text clipped for the page budget.
    work_item_id: int
    title: str
    source: str
    ticket_ref: str | None = None
    repo: str
    pr_number: int | None = None
    branch: str | None = None
    # The handoff lane at rest (scoped/shipped/skipped/cancelled); None while
    # the item is in flight.
    lane: str | None = None
    links: Links
    status: SubjectStatus
    updated_at: datetime

    @classmethod
    def from_work_item(cls, item: WorkItem, status: SubjectStatus) -> "AgentWorkItem":
        return cls(
            work_item_id=item.id,
            title=clip(item.title, _TITLE_CLIP) or "",
            source=item.source,
            ticket_ref=item.remote_key,
            repo=item.repo,
            pr_number=item.pr_number,
            branch=item.branch,
            lane=item.status,
            links=Links.from_work_item(item, ticket_clip=_URL_CLIP),
            status=_bounded_status(status),
            updated_at=item.updated_at,
        )


class AgentWorkPage(BaseResponse):
    items: list[AgentWorkItem] = Field(default_factory=list)
    # Opaque keyset position; absent on the last page.
    next_cursor: str | None = None


class AgentWorkItemDetail(BaseResponse):
    work_item_id: int
    title: str
    source: str
    project_name: str
    ticket_ref: str | None = None
    repo: str
    pr_number: int | None = None
    branch: str | None = None
    lane: str | None = None
    links: Links
    status: SubjectStatus
    created_at: datetime
    updated_at: datetime
    # Newest first, each with its latest agent calls.
    runs: list[RunSummary] = Field(default_factory=list)

    @classmethod
    def from_work_item(
        cls, item: WorkItem, status: SubjectStatus, runs: list[RunSummary]
    ) -> "AgentWorkItemDetail":
        return cls(
            work_item_id=item.id,
            title=clip(item.title, _TITLE_CLIP) or "",
            source=item.source,
            project_name=item.project.name,
            ticket_ref=item.remote_key,
            repo=item.repo,
            pr_number=item.pr_number,
            branch=item.branch,
            lane=item.status,
            links=Links.from_work_item(item, ticket_clip=_URL_CLIP),
            status=_bounded_status(status),
            created_at=item.created_at,
            updated_at=item.updated_at,
            runs=runs,
        )


class AgentDispatchResult(BaseResponse):
    work_item_id: int
    run_id: str
    is_owned_by_caller: bool
    note: str
