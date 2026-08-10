import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Response, status
from sqlalchemy import delete, func, select, update

from druks.accounts.dependencies import current_account
from druks.accounts.models import Account
from druks.api.exceptions import agent_error_responses
from druks.contrib.ship.exceptions import TicketNotFound, TrackerNotConfigured
from druks.contrib.ship.extension import Ship
from druks.contrib.ship.models import Project, ProjectRepo, WorkItem
from druks.contrib.ship.schemas import (
    AddProjectRepoRequest,
    CreateProjectRequest,
    DashboardItem,
    GitHubReposResponse,
    GitHubRepoSummary,
    ProjectRepoSummary,
    ProjectsResponse,
    ProjectSummary,
    WorkItemsHistoryResponse,
)
from druks.contrib.ship.ticketing.enums import TicketStatus
from druks.contrib.ship.workflows import Profile
from druks.core.apis.exceptions import UnknownTicketError
from druks.core.apis.github import get_github_client
from druks.db import db_session
from druks.services.exceptions import ServiceNotConnectedError

logger = logging.getLogger(__name__)


# /api/ship/projects                                          Project / ProjectRepo

projects_router = APIRouter(prefix="/projects", tags=["projects"])


@projects_router.get("", response_model=ProjectsResponse, response_model_by_alias=True)
async def list_projects() -> ProjectsResponse:
    rows = list(db_session().scalars(select(Project).order_by(Project.name)))
    return ProjectsResponse(projects=[ProjectSummary.model_validate(p) for p in rows])


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
    return ProjectSummary.model_validate(project)


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
    github = get_github_client()
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
    return ProjectSummary.model_validate(project)


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
    return ProjectSummary.model_validate(project)


@projects_router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(project_id: int) -> None:
    """Delete a project and everything it owns. A project's work items are its own
    children, so they go with it — ``work_items.project_id`` is a plain FK, so the
    cascade is an explicit child delete in the same session before the project (and
    its repo ``delete-orphan`` cascade) is deleted."""
    session = db_session()
    project = Project.get(project_id)
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    session.execute(delete(WorkItem).where(WorkItem.project_id == project_id))
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
    try:
        await Profile.dispatch(repo)
    except ServiceNotConnectedError:
        # Registering the repo is metadata and must survive; profiling needs the
        # operator App, so defer it — a later push or a manual profile picks it up.
        logger.info("Registered %s without profiling: GitHub is not connected.", full_name)
    return ProjectRepoSummary.model_validate(repo)


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
    return ProjectRepoSummary.model_validate(row)


@projects_router.post(
    "/{project_id}/repos/{repo_id}/profile",
    response_model=ProjectRepoSummary,
    response_model_by_alias=True,
)
async def profile_project_repo(project_id: int, repo_id: int) -> ProjectRepoSummary:
    row = ProjectRepo.get_in_project(project_id=project_id, repo_id=repo_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "repo not found")
    # Profile is subject-unique: dispatch() returns the live run when one is already
    # active for this repo, so the route just dispatches and lets the lock dedup.
    await Profile.dispatch(row)
    return ProjectRepoSummary.model_validate(row)


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


# /api/ship/work-items                                                WorkItem CRUD

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
    # Recent history: the PRs GitHub has resolved, its verdict newest first.
    items = [DashboardItem.model_validate(wi) for wi in WorkItem.list_handoff(limit=clamped)]
    return WorkItemsHistoryResponse(items=items)


@work_items_router.post(
    "/{ticket}/start",
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="ship_start",
    tags=["agent"],
    responses=agent_error_responses(TicketNotFound("ENG-9999", "Linear"), TrackerNotConfigured()),
)
async def start_work_item(
    ticket: str = Path(
        ...,
        description="The tracker's ticket key, e.g. ENG-831.",
    ),
    account: Account = Depends(current_account),
) -> None:
    """Move the tracker ticket to the configured trigger status; webhook intake
    then opens the build. No run exists yet when this returns — poll
    list_open_subjects while waiting for it."""
    tracker = Ship.get_tracker()
    if not tracker:
        raise TrackerNotConfigured()
    async with tracker:
        try:
            await tracker.set_status(ticket, TicketStatus.TRIGGER)
        except UnknownTicketError as error:
            raise TicketNotFound(error.key, error.tracker) from error
