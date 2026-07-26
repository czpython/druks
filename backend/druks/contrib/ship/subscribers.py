from druks.contrib.ship.contracts import ReviewWork
from druks.contrib.ship.enums import HandoffStatus
from druks.contrib.ship.extension import Ship
from druks.contrib.ship.models import ProjectRepo, WorkItem
from druks.contrib.ship.workflows import Build, Profile
from druks.signals import subscribe
from druks.ticketing.enums import TicketStatus
from druks.workflows import get_subject_status

# Projections


@subscribe("run.running", subject=WorkItem)
async def run_start_returns_item_to_board(*, subject: WorkItem, **_: object) -> None:
    # Any run starting for a work item puts it back on the active board —
    # a new build or resume means druks has it in court again.
    subject.set_status(None)


@subscribe("run.state", workflow=Build, subject=WorkItem)
async def provision_mirrors_onto_item(
    *, subject: WorkItem, pr_number: int, branch: str, **_: object
) -> None:
    # The implementer's provisioned PR + branch, mirrored onto the work item —
    # the read side (board links, webhook routing by repo+PR) keys off them.
    subject.update(pr_number=pr_number, branch=branch)


@subscribe("run.running", workflow=Build, subject=WorkItem)
async def build_start_marks_ticket_in_progress(*, subject: WorkItem, **_: object) -> None:
    # Every (re)start and gate-resume of a build means the ticket is in progress —
    # including the return from a rework loop that had parked it In Review.
    await subject.set_remote_status(TicketStatus.IN_PROGRESS)


@subscribe("run.pending_input", workflow=Build, gate=ReviewWork, subject=WorkItem)
async def review_park_marks_ticket_in_review(*, subject: WorkItem, **_: object) -> None:
    await subject.set_remote_status(TicketStatus.IN_REVIEW)


@subscribe("run.failed", workflow=Build, subject=WorkItem)
@subscribe("run.cancelled", workflow=Build, subject=WorkItem)
async def build_end_settles_the_item(*, subject: WorkItem, **_: object) -> None:
    # Nothing merged, so the attempt was abandoned — unless the PR already spoke:
    # ship() cancels the run it just shipped, and that cancel arrives here.
    if not subject.status:
        subject.set_status(HandoffStatus.CANCELLED)


@subscribe("repo.pushed", to_default_branch=True)
async def policy_push_reprofiles_the_repo(*, repo: str, paths: list, **_: object) -> None:
    # The operator edited the repo's build policy — re-apply it over the
    # profiled baseline.
    if ".druks/ship/config.yml" in paths:
        project_repo = ProjectRepo.get_for_repo(repo)

        if project_repo:
            await Profile.dispatch(project_repo, refresh_only=True)


# Routers


@subscribe("pr.review_submitted")
async def pr_review_answers_the_gate(*, repo: str, pr_number: int, payload: dict) -> None:
    item = WorkItem.get_for_pr(repo=repo, pr_number=pr_number, branch=payload["branch"])
    if not item:
        return
    status = get_subject_status(item.subject_type, str(item.id), workflow=Build)
    if status.is_parked and status.gate == ReviewWork.name:
        await ReviewWork.answer(
            item,
            action=payload["action"],
            reviewer=payload["reviewer"],
            body=payload["body"],
        )


@subscribe("pr.closed")
async def pr_close_settles_the_item(*, repo: str, pr_number: int, payload: dict) -> None:
    """A PR druks owns closed on GitHub — the owner announcing the outcome.
    One path for every merge, druks's own included: GitHub says merged, druks
    ships the item. The status guards are redelivery idempotency."""
    item = WorkItem.get_for_pr(repo=repo, pr_number=pr_number, branch=payload["branch"])
    if not item or item.status == HandoffStatus.SHIPPED:
        return
    if payload["merged"]:
        await item.ship()
    elif item.status != HandoffStatus.CANCELLED:
        await item.close_external()


@subscribe("ticket.transitioned")
async def ticket_transition_drives_the_funnel(*, payload: dict) -> None:
    """Dispatch a build when a tracker ticket enters its provider's trigger status."""
    source, status = payload["source"], payload["status"]
    settings = Ship.settings()
    trigger = settings.linear_trigger_status if source == "linear" else settings.jira_trigger_status
    if trigger and status == trigger:
        await Build.dispatch(ticket=payload)
