import logging
from typing import cast

from fastapi import APIRouter, Body, HTTPException, Path, Query, Response, status
from sqlalchemy import func, select, update

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
    ShipStartResponse,
    WorkItemsHistoryResponse,
)
from druks.contrib.ship.ticketing.exceptions import (
    TrackerStatusUnavailable,
    TrackerTicketNotFound,
)
from druks.contrib.ship.workflows import Profile
from druks.core.apis.github import get_github_client
from druks.db import db_session
from druks.settings import load_settings

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
    await Profile.dispatch(repo)
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
    response_model=ShipStartResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="ship_start",
    tags=["agent"],
    responses={
        404: {
            "description": "Unknown ticket.",
            "content": {
                "application/json": {
                    "example": {
                        "error": "HTTP_404",
                        "detail": "Linear knows no ENG-9999",
                    }
                }
            },
        },
        409: {
            "description": "Tracker configuration or workflow status conflict.",
            "content": {
                "application/json": {
                    "examples": {
                        "no_tracker": {
                            "value": {
                                "error": "HTTP_409",
                                "detail": "No ticket tracker is configured.",
                            }
                        },
                        "linear_not_configured": {
                            "value": {
                                "error": "HTTP_409",
                                "detail": "Linear is not configured.",
                            }
                        },
                        "jira_not_configured": {
                            "value": {
                                "error": "HTTP_409",
                                "detail": "Jira is not configured.",
                            }
                        },
                        "trigger_status_not_configured": {
                            "value": {
                                "error": "HTTP_409",
                                "detail": "Linear trigger status is not configured.",
                            }
                        },
                        "status_unavailable": {
                            "value": {
                                "error": "HTTP_409",
                                "detail": (
                                    "Linear cannot move ENG-833 to status 'Ready for Agent'."
                                ),
                            }
                        },
                    }
                }
            },
        },
        502: {
            "description": "Tracker request failed.",
            "content": {
                "application/json": {
                    "example": {
                        "error": "HTTP_502",
                        "detail": (
                            "Linear could not move ENG-833 to the build-trigger status; ask the "
                            "operator to check tracker access and availability."
                        ),
                    }
                }
            },
        },
    },
)
async def start_work_item(
    ticket: str = Path(
        ...,
        description=(
            "Tracker ticket key in uppercase PROJECT-NUMBER form, e.g. ENG-833. It need not "
            "yet appear in list_open_subjects; lowercase and surrounding whitespace are rejected."
        ),
        max_length=64,
        pattern=r"^[A-Z][A-Z0-9]*-[1-9][0-9]*$",
    ),
) -> ShipStartResponse:
    # fmt: off
    (
        "Move a tracker ticket into the configured build-trigger status. `stamped` means this "
        "call changed the tracker; poll list_open_subjects for webhook intake, but a 202 still "
        "does not confirm a build will start. `already_stamped` means the ticket was already in "
        "the trigger status, so this call emitted no webhook; work may already be in the funnel "
        "from an earlier stamp, or the ticket may have been resting there without Druks ever "
        "ingesting it. Poll list_open_subjects after either result and never re-issue ship_start. "
        "If work never appears, intake may have declined unroutable or already-merged work, "
        "webhook delivery may have been lost, or an already-stamped ticket may never have been "
        "ingested; this is terminal for the caller, so escalate rather than retry. Calling "
        "ship_start on a ticket already being built moves the tracker back to the trigger status "
        "without starting a second build, and the tracker will misreport until the run's next "
        "lifecycle event."
    )
    # fmt: on
    settings = cast(Ship.Settings, Ship.settings())
    if settings.tracker == "none":
        raise HTTPException(status.HTTP_409_CONFLICT, "No ticket tracker is configured.")

    tracker_name = settings.tracker.title()
    if not settings.trigger_status.strip():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{tracker_name} trigger status is not configured.",
        )

    tracker = Ship.tracker(settings.tracker)
    if not tracker:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{tracker_name} is not configured.")

    try:
        async with tracker:
            changed = await tracker.move_ticket(ticket, settings.trigger_status)
    except TrackerTicketNotFound as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
    except TrackerStatusUnavailable as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    except tracker.known_exceptions as error:
        logger.warning(
            "%s could not move ticket %s to the build-trigger status.",
            tracker_name,
            ticket,
            exc_info=True,
        )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"{tracker_name} could not move {ticket} to the build-trigger status; ask the "
            "operator to check tracker access and availability.",
        ) from error

    return ShipStartResponse(result="stamped" if changed else "already_stamped")
