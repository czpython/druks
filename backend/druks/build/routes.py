import logging
from typing import Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from sqlalchemy import func, select, update

from druks.accounts.dependencies import current_account
from druks.accounts.models import Account
from druks.build import agent
from druks.build.models import Project, ProjectRepo, WorkItem
from druks.build.schemas import (
    AddProjectRepoRequest,
    AgentDispatchResult,
    AgentWorkItemDetail,
    AgentWorkPage,
    CreateProjectRequest,
    DashboardItem,
    GitHubReposResponse,
    GitHubRepoSummary,
    ProjectRepoSummary,
    ProjectsResponse,
    ProjectSummary,
    WorkItemsHistoryResponse,
)
from druks.build.workflows import Profile
from druks.core.apis.github import get_github_client
from druks.db import db_session
from druks.settings import load_settings

logger = logging.getLogger(__name__)


# /api/build/projects                                          Project / ProjectRepo

projects_router = APIRouter(prefix="/projects", tags=["projects"])


@projects_router.get("", response_model=ProjectsResponse, response_model_by_alias=True)
async def list_projects() -> ProjectsResponse:
    rows = list(db_session().scalars(select(Project).order_by(Project.name)))
    return ProjectsResponse(projects=[ProjectSummary.from_project(p) for p in rows])


@projects_router.post(
    "",
    response_model=ProjectSummary,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
)
async def create_project(body: CreateProjectRequest) -> ProjectSummary:
    name = body.name.strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "name is required")
    project = Project.create(name=name)
    return ProjectSummary.from_project(project)


# GitHub repo typeahead source. Declared BEFORE the ``/{project_id}``
# routes so FastAPI's order-sensitive matcher doesn't try to parse
# ``github-repos`` as an int project_id.
@projects_router.get(
    "/github-repos",
    response_model=GitHubReposResponse,
    response_model_by_alias=True,
)
async def list_github_repos(
    owner: str | None = Query(
        default=None,
        description=(
            "GitHub owner to filter by. Default: every repo across the "
            "operator App's installations."
        ),
    ),
) -> GitHubReposResponse:
    settings = load_settings()
    github = get_github_client(settings)
    resolved = (owner or "").strip()
    if resolved:
        owners: tuple[str, ...] = (resolved,)
    else:
        owners = await github.list_installation_accounts()
        if not owners:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "the operator GitHub App has no installations — install it on your org",
            )
    repos = [
        repo
        for account in sorted(owners, key=str.casefold)
        for repo in await github.list_repos_for_owner(account)
    ]
    return GitHubReposResponse(
        repos=[
            GitHubRepoSummary(full_name=r["full_name"], description=r.get("description"))
            for r in repos
        ],
    )


@projects_router.get(
    "/{project_id}",
    response_model=ProjectSummary,
    response_model_by_alias=True,
)
async def get_project(project_id: int) -> ProjectSummary:
    project = Project.get(project_id)
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    return ProjectSummary.from_project(project)


@projects_router.patch(
    "/{project_id}",
    response_model=ProjectSummary,
    response_model_by_alias=True,
)
async def update_project(
    project_id: int,
    name: str | None = Body(default=None, embed=True),
) -> ProjectSummary:
    project = Project.get(project_id)
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    if name is not None:
        name = name.strip()
        if not name:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "name cannot be empty")
        project.name = name
        db_session().flush()
    return ProjectSummary.from_project(project)


@projects_router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(project_id: int) -> None:
    """Delete a project. Refuses when any WorkItem still points at it —
    ``work_items.project_id`` is NOT NULL, so the operator must move
    or delete the children first."""
    session = db_session()
    project = Project.get(project_id)
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    referencing = session.scalar(
        select(func.count()).select_from(WorkItem).where(WorkItem.project_id == project_id)
    )
    if referencing:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{referencing} work item(s) still reference this project; move or delete them first.",
        )
    session.delete(project)
    session.flush()


@projects_router.post(
    "/{project_id}/repos",
    response_model=ProjectRepoSummary,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
)
async def add_project_repo(
    project_id: int,
    body: AddProjectRepoRequest,
) -> ProjectRepoSummary:
    project = Project.get(project_id)
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    full_name = body.full_name.strip()
    if not full_name or "/" not in full_name:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "fullName must be 'owner/name'",
        )
    repo = ProjectRepo.create(
        project_id=project.id,
        full_name=full_name,
        purpose=body.purpose,
    )
    # Re-bind matching work items that still point at a different project.
    db_session().execute(
        update(WorkItem)
        .where(
            func.lower(WorkItem.repo) == full_name.lower(),
            WorkItem.project_id != project.id,
        )
        .values(project_id=project.id)
    )
    await Profile.start(subject={"type": "project_repo", "id": repo.id}, repo_id=repo.id)
    return ProjectRepoSummary.from_repo(repo)


@projects_router.patch(
    "/{project_id}/repos/{repo_id}",
    response_model=ProjectRepoSummary,
    response_model_by_alias=True,
)
async def update_project_repo(
    project_id: int,
    repo_id: int,
    purpose: str | None = Body(default=None, embed=True),
) -> ProjectRepoSummary:
    row = ProjectRepo.get_in_project(project_id=project_id, repo_id=repo_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "repo not found")
    if purpose is not None:
        row.purpose = purpose.strip() or None
        db_session().flush()
    return ProjectRepoSummary.from_repo(row)


@projects_router.post(
    "/{project_id}/repos/{repo_id}/profile",
    response_model=ProjectRepoSummary,
    response_model_by_alias=True,
)
async def profile_project_repo(project_id: int, repo_id: int) -> ProjectRepoSummary:
    row = ProjectRepo.get_in_project(project_id=project_id, repo_id=repo_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "repo not found")
    # Profile is subject-unique: start() returns the live run when one is already
    # active for this repo, so the route just dispatches and lets the lock dedup.
    await Profile.start(subject={"type": "project_repo", "id": repo_id}, repo_id=repo_id)
    return ProjectRepoSummary.from_repo(row)


@projects_router.delete(
    "/{project_id}/repos/{repo_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_project_repo(project_id: int, repo_id: int) -> None:
    row = ProjectRepo.get_in_project(project_id=project_id, repo_id=repo_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "repo not found")
    session = db_session()
    session.delete(row)
    session.flush()


# /api/build/work-items                                                WorkItem CRUD

work_items_router = APIRouter(prefix="/work-items", tags=["work-items"])

# History endpoint cap. 500 covers months of activity for an active
# operator without risking a runaway payload. Above this we ship the
# most-recent slice and add a "load older" affordance later.
_HISTORY_DEFAULT_LIMIT = 200
_HISTORY_MAX_LIMIT = 500


@work_items_router.get(
    "/history",
    response_model=WorkItemsHistoryResponse,
    response_model_by_alias=True,
)
async def list_work_items_history(
    response: Response,
    limit: int = _HISTORY_DEFAULT_LIMIT,
) -> WorkItemsHistoryResponse:
    response.headers["Cache-Control"] = "no-store"
    clamped = max(1, min(limit, _HISTORY_MAX_LIMIT))
    # Recent-history aggregation. History is "handoff" — druks finished a
    # unit and handed off (shipped / cancelled / skipped / scoped). list_handoff
    # reads the event log directly, already ordered newest-handoff-first and bounded.
    items = [DashboardItem.from_work_item(wi) for wi in WorkItem.list_handoff(limit=clamped)]
    return WorkItemsHistoryResponse(items=items)


# /api/build/agent                                            the agent surface

agent_router = APIRouter(prefix="/agent", tags=["agent"])


class DispatchRequest(BaseModel):
    # Address the item one way: its druks id, or its ticket in the tracker.
    work_item_id: int | None = None
    source: Literal["linear", "jira"] | None = None
    ticket_ref: str | None = None


@agent_router.get(
    "/work",
    operation_id="list_work",
    response_model=AgentWorkPage,
    response_model_by_alias=True,
)
async def list_work(
    filter: Literal["mine", "parked", "active", "failed"] | None = None,
    cursor: str | None = None,
    account: Account = Depends(current_account),
) -> AgentWorkPage:
    return agent.list_work(account, filter=filter, cursor=cursor)


@agent_router.get(
    "/work-items/{work_item_id}",
    operation_id="get_work_item",
    response_model=AgentWorkItemDetail,
    response_model_by_alias=True,
)
async def get_work_item(work_item_id: int) -> AgentWorkItemDetail:
    return agent.get_work_item(work_item_id)


@agent_router.post(
    "/dispatch",
    operation_id="dispatch",
    response_model=AgentDispatchResult,
    response_model_by_alias=True,
)
async def dispatch(
    body: DispatchRequest, account: Account = Depends(current_account)
) -> AgentDispatchResult:
    if bool(body.work_item_id) == bool(body.source and body.ticket_ref):
        # The addressing XOR is request shape, so it wears Pydantic's envelope.
        raise RequestValidationError(
            [
                {
                    "loc": ("body",),
                    "msg": "Address exactly one of work_item_id, or source + ticket_ref.",
                    "type": "value_error",
                }
            ]
        )
    return await agent.dispatch(
        account,
        work_item_id=body.work_item_id,
        source=body.source,
        ticket_ref=body.ticket_ref,
    )
