import logging
import re

from githubkit.exception import RequestFailed, RequestTimeout

from druks.build.enums import HandoffStatus
from druks.build.extension import Build
from druks.build.models import Project, ProjectRepo, WorkItem
from druks.build.policy import RepoPolicy
from druks.build.scoping.workflows import Scope
from druks.build.workflows import BuildWorkflow, Profile, _delete_branch
from druks.core.apis.exceptions import GitHubAppNotConfiguredError, GitHubAppNotInstalledError
from druks.core.apis.github import get_github_client
from druks.settings import load_settings
from druks.signals import subscribe
from druks.ticketing.enums import SemanticStatus
from druks.ticketing.exceptions import TrackerNotConfigured
from druks.ticketing.helpers import get_tracker
from druks.workflows import Run, RunState

logger = logging.getLogger(__name__)

_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")


@subscribe("run.running", subject__type="work_item")
async def work_item_back_on_board(*, subject: dict, **_: object) -> None:
    # Any run starting for a work item puts it back on the active board —
    # re-scoping, a new build, a resume all mean druks has it in court again.
    WorkItem.get(subject["id"]).set_status(None)


@subscribe("run.finished", kind=Scope.kind, subject__type="work_item", result__status="ready")
async def scope_outcome_settles_lane(*, subject: dict, **_: object) -> None:
    WorkItem.get(subject["id"]).set_status(HandoffStatus.SCOPED, event_payload={})


@subscribe("repo.pushed", to_default_branch=True)
async def policy_push_reprofiles_the_repo(*, repo: str, paths: list, **_: object) -> None:
    # The operator edited the repo's build policy — re-apply it over the
    # profiled baseline.
    if ".druks/build/config.yml" not in paths:
        return
    project_repo = ProjectRepo.get_for_repo(repo)
    if not project_repo:
        return
    await Profile.start(
        subject={"type": "project_repo", "id": project_repo.id},
        repo_id=project_repo.id,
        refresh_only=True,
    )


@subscribe("run.state", kind=BuildWorkflow.kind, subject__type="work_item")
async def provision_reaches_the_work_item(
    *, subject: dict, pr_number: int, branch: str, **_: object
) -> None:
    # The implementer's provisioned PR + branch, mirrored onto the work item —
    # the read side (board links, webhook routing by repo+PR) keys off them.
    WorkItem.get(subject["id"]).update(pr_number=pr_number, branch=branch)


@subscribe("run.running", kind=BuildWorkflow.kind, subject__type="work_item")
async def build_running_reaches_the_tracker(*, subject: dict, **_: object) -> None:
    # Every (re)start and gate-resume of a build means the ticket is in progress —
    # including the return from a rework loop that had parked it In Review.
    item = WorkItem.get(subject["id"])
    await item.set_remote_status(SemanticStatus.IN_PROGRESS)


@subscribe(
    "run.pending_input", kind=BuildWorkflow.kind, subject__type="work_item", gate="review_work"
)
async def work_review_park_reaches_the_tracker(*, subject: dict, **_: object) -> None:
    item = WorkItem.get(subject["id"])
    await item.set_remote_status(SemanticStatus.IN_REVIEW)


@subscribe("pr.review_submitted")
async def route_pr_review(*, repo: str, pr_number: int, payload: dict) -> None:
    if not WorkItem.is_known_druks_pr(repo=repo, pr_number=pr_number, branch=payload["branch"]):
        return

    run = _active_build_run_for_pr(repo, pr_number)
    if not run or not run.input_gate:
        return
    await run.resume(action=payload["action"], reviewer=payload["reviewer"], body=payload["body"])


def _active_build_run_for_pr(repo: str, pr_number: int) -> "Run | None":
    item = WorkItem.get_for_pr(repo=repo, pr_number=pr_number)
    if not item:
        return None
    run = item.get_build_run()
    return run if run and run.is_active else None


@subscribe("pr.closed")
async def observe_pr_closed(*, repo: str, pr_number: int, payload: dict) -> None:
    """A PR druks owns closed on GitHub — the owner announcing the outcome.
    One path for every merge, druks's own included: GitHub says merged, druks
    ships the item. The status guards are redelivery idempotency."""
    if not WorkItem.is_known_druks_pr(
        repo=repo, pr_number=pr_number, branch=payload["branch"], include_terminal=True
    ):
        return
    work_item = WorkItem.get_for_pr(repo=repo, pr_number=pr_number)
    status = work_item.status if work_item else None
    if status == HandoffStatus.SHIPPED:
        return
    if payload["merged"]:
        await _ship(repo=repo, pr_number=pr_number, work_item=work_item)
    elif status != HandoffStatus.CANCELLED:
        await _observe_external_close(repo=repo, pr_number=pr_number, work_item=work_item)


async def _ship(*, repo: str, pr_number: int, work_item: WorkItem | None) -> None:
    if not work_item:
        return
    work_item.set_status(HandoffStatus.SHIPPED)
    # An operator merge over a waiting gate strands the parked run — cancel it.
    # A RUNNING run converges on its own (its merge step sees the closed PR), so
    # it is left to finish; that includes druks's own merge wrapping up.
    run = work_item.get_build_run()
    if run and run.state == RunState.PENDING_INPUT.value:
        await run.cancel(failure="pr merged while parked")
    await work_item.set_remote_status(SemanticStatus.DONE)
    logger.info("Merge observed for %s#%s.", repo, pr_number)


async def _observe_external_close(*, repo: str, pr_number: int, work_item: WorkItem | None) -> None:
    if not work_item:
        return
    work_item.set_status(HandoffStatus.CANCELLED, event_payload={"external": True})
    await work_item.cancel_active_build(failure="pr closed without merge")
    snapshot_policy = work_item.extension_config_snapshot.get("policy") or {}
    if RepoPolicy.model_validate(snapshot_policy).delete_branch:
        await _delete_branch(repo, work_item.branch)
    # The attempt was abandoned, not the ticket: send it back to the
    # provider's resting pool rather than stranding it in In Progress.
    await work_item.set_remote_status(SemanticStatus.READY_FOR_AGENT)
    logger.info("External close observed for %s#%s.", repo, pr_number)


@subscribe("ticket.transitioned")
async def route_ticket_transition(*, payload: dict) -> None:
    """A tracker ticket changed state (Jira or Linear). Scope a refinement
    candidate and open a build when it hits the trigger status — build's whole
    tracker-driven funnel."""
    source, status, key = payload["source"], payload["status"], payload["identifier"]
    settings = Build.settings()
    if status in settings.scoper_candidate_statuses:
        await _dispatch_scope(source, key)
    trigger = settings.linear_trigger_status if source == "linear" else settings.jira_trigger_status
    if trigger and status == trigger:
        await _dispatch_intake(source, payload)


@subscribe("ticket.commented")
async def route_ticket_comment(*, payload: dict) -> None:
    """An operator's reply on a ticket with a parked scope run — resume it; the
    agent re-reads the whole thread, so which comment was answered is irrelevant."""
    if not payload["parent_id"]:
        return  # top-level comment, not a reply
    async with get_tracker(payload["source"]) as tracker:
        # Linear's GraphQL takes the issue UUID wherever it takes the key.
        ticket = await tracker.fetch_ticket(payload["issue_id"])
    item = WorkItem.get_for_remote_key(source=payload["source"], remote_key=ticket.key)
    if not item:
        return
    if parked := Scope.parked_for(item.id):
        await parked.resume()


@subscribe("ticket.transitioned", payload__terminal=True)
async def ticket_close_cancels_parked_scope(*, payload: dict) -> None:
    """The operator moved the ticket to a terminal status while a scope run was
    parked on it — nobody is left to answer the gate, so end the run now instead
    of at the gate TTL."""
    item = WorkItem.get_for_remote_key(source=payload["source"], remote_key=payload["identifier"])
    if not item:
        return
    parked = Scope.parked_for(item.id)
    if not parked:
        return
    item.set_status(HandoffStatus.CANCELLED, event_payload={"external": True})
    await parked.cancel(failure="ticket closed while scope parked")


async def _dispatch_scope(source: str, key: str) -> None:
    try:
        tracker = get_tracker(source)
    except TrackerNotConfigured:
        return
    async with tracker:
        ticket = await tracker.fetch_ticket(key)
        await Scope.dispatch(ticket=ticket)


async def _dispatch_intake(source: str, payload: dict) -> None:
    key = payload["identifier"]
    item = WorkItem.get_for_remote_key(source=source, remote_key=key)
    if item:
        # Re-intake: refresh what the tracker may have changed since creation.
        item.update(title=payload["title"], remote_url=payload["url"])
    else:
        repo, project_id = await _resolve_repo(source, payload)
        if not repo:
            logger.info("Ticket %s has no routable repo; skipping intake.", key)
            return
        item = WorkItem.create(
            project_id=project_id,
            source=source,
            title=payload["title"],
            remote_key=key,
            remote_url=payload["url"],
            repo=repo,
        )

    await BuildWorkflow.dispatch(
        work_item_id=item.id,
        assignee_email=payload["assignee_email"],
        assignee_name=payload["assignee_name"],
    )


async def _resolve_repo(source: str, payload: dict) -> tuple[str | None, int | None]:
    if source == "jira":
        target = ProjectRepo.lookup(project_name=payload["project_name"], labels=payload["labels"])
        return (target.full_name, target.project_id) if target else (None, None)
    # Linear: the project name is the bare repo name; resolve the owner by
    # probing the operator Extension's installations.
    repo = await _accessible_repo_for_project(payload["project_name"])
    project = Project.get_for_repo(repo) if repo else None
    return (repo, project.id) if project else (None, None)


async def _accessible_repo_for_project(project_name: str | None) -> str | None:
    candidates = await _candidate_repos(project_name)
    if len(candidates) <= 1:
        return candidates[0] if candidates else None

    # Candidates exist only when the Extension is configured, so the client is too.
    github = get_github_client(load_settings())
    accessible = []
    for repo in candidates:
        try:
            await github.get_repository(repo)
        except (GitHubAppNotInstalledError, RequestFailed, RequestTimeout):
            continue  # uninstalled/404/403/network — inaccessible, keep probing
        accessible.append(repo)
    return accessible[0] if len(accessible) == 1 else None


async def _candidate_repos(project_name: str | None) -> list[str]:
    name = (project_name or "").strip()
    if not name or not _REPO_SLUG_RE.match(name):
        return []

    try:
        github = get_github_client(load_settings())
    except GitHubAppNotConfiguredError:
        return []

    try:
        owners = await github.list_installation_accounts()
    except Exception:  # noqa: BLE001 — best-effort; no installations → no candidates
        logger.warning("Could not list Extension installations for repo resolution.", exc_info=True)
        return []
    return [f"{owner}/{name}" for owner in sorted(owners, key=str.casefold)]
