from druks.build.contracts import ReviewWork
from druks.build.enums import HandoffStatus
from druks.build.extension import Build
from druks.build.models import ProjectRepo, WorkItem
from druks.build.workflows import BuildWorkflow, Profile, Scope
from druks.signals import subscribe
from druks.ticketing.enums import SemanticStatus
from druks.ticketing.exceptions import TrackerNotConfigured
from druks.ticketing.helpers import get_tracker
from druks.workflows import Run

# Projections


@subscribe("run.running", subject__type="work_item")
async def run_start_returns_item_to_board(*, subject: dict, **_: object) -> None:
    # Any run starting for a work item puts it back on the active board —
    # re-scoping, a new build, a resume all mean druks has it in court again.
    WorkItem.get(subject["id"]).set_status(None)


@subscribe("run.state", kind=BuildWorkflow.kind, subject__type="work_item")
async def provision_mirrors_onto_item(
    *, subject: dict, pr_number: int, branch: str, **_: object
) -> None:
    # The implementer's provisioned PR + branch, mirrored onto the work item —
    # the read side (board links, webhook routing by repo+PR) keys off them.
    WorkItem.get(subject["id"]).update(pr_number=pr_number, branch=branch)


@subscribe("run.running", kind=BuildWorkflow.kind, subject__type="work_item")
async def build_start_marks_ticket_in_progress(*, subject: dict, **_: object) -> None:
    # Every (re)start and gate-resume of a build means the ticket is in progress —
    # including the return from a rework loop that had parked it In Review.
    item = WorkItem.get(subject["id"])
    await item.set_remote_status(SemanticStatus.IN_PROGRESS)


@subscribe("run.pending_input", kind=BuildWorkflow.kind, subject__type="work_item", gate=ReviewWork)
async def review_park_marks_ticket_in_review(*, subject: dict, **_: object) -> None:
    item = WorkItem.get(subject["id"])
    await item.set_remote_status(SemanticStatus.IN_REVIEW)


@subscribe("run.finished", kind=Scope.kind, subject__type="work_item", result__status="ready")
async def scope_ready_settles_the_lane(*, subject: dict, **_: object) -> None:
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


# Routers


@subscribe("pr.review_submitted")
async def pr_review_answers_the_gate(*, repo: str, pr_number: int, payload: dict) -> None:
    if not WorkItem.is_known_druks_pr(repo=repo, pr_number=pr_number, branch=payload["branch"]):
        return

    run = _active_build_run_for_pr(repo, pr_number)
    if not run or not run.input_gate:
        return
    await run.resume(action=payload["action"], reviewer=payload["reviewer"], body=payload["body"])


def _active_build_run_for_pr(repo: str, pr_number: int) -> "Run | None":
    item = WorkItem.get_for_pr(repo=repo, pr_number=pr_number)
    if not item:
        return
    run = item.get_build_run()
    return run if run and run.is_active else None


@subscribe("pr.closed")
async def pr_close_settles_the_item(*, repo: str, pr_number: int, payload: dict) -> None:
    """A PR druks owns closed on GitHub — the owner announcing the outcome.
    One path for every merge, druks's own included: GitHub says merged, druks
    ships the item. The status guards are redelivery idempotency."""
    if not WorkItem.is_known_druks_pr(
        repo=repo, pr_number=pr_number, branch=payload["branch"], include_terminal=True
    ):
        return
    item = WorkItem.get_for_pr(repo=repo, pr_number=pr_number)
    if item:
        if item.status == HandoffStatus.SHIPPED:
            return
        if payload["merged"]:
            await item.ship()
        elif item.status != HandoffStatus.CANCELLED:
            await item.close_external()


@subscribe("ticket.transitioned")
async def ticket_transition_drives_the_funnel(*, payload: dict) -> None:
    """A tracker ticket changed state (Jira or Linear). Scope a refinement
    candidate and open a build when it hits the trigger status — build's whole
    tracker-driven funnel."""
    source, status, key = payload["source"], payload["status"], payload["identifier"]
    settings = Build.settings()
    if status in settings.scoper_candidate_statuses:
        await _dispatch_scope(source, key)
    trigger = settings.linear_trigger_status if source == "linear" else settings.jira_trigger_status
    if trigger and status == trigger:
        await BuildWorkflow.dispatch(ticket=payload)


@subscribe("ticket.commented")
async def ticket_reply_resumes_parked_scope(*, payload: dict) -> None:
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
