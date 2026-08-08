from druks.contrib.ship.contracts import ReviewWork
from druks.contrib.ship.extension import Ship
from druks.contrib.ship.models import ProjectRepo, WorkItem
from druks.contrib.ship.ticketing.enums import TicketStatus
from druks.contrib.ship.workflows import Build, Profile
from druks.db import Base
from druks.signals import subscribe
from druks.workflows import WorkflowEvent


@subscribe(WorkflowEvent.SCHEDULED, workflow=Build)
async def new_build_claims_the_item(*, subject: WorkItem, **_: object) -> None:
    subject.start_attempt()


@subscribe(WorkflowEvent.CANCELLED, workflow=Build)
async def cancelled_build_settles_the_item(*, subject: WorkItem, **_: object) -> None:
    """An operator cancellation explicitly abandons the work item."""
    if not subject.resolution:
        subject.resolve(merged=False, at=Base.utc_now())


@subscribe("pr.opened", workflow=Build)
async def pr_open_mirrors_onto_item(
    *, subject: WorkItem, pr_number: int, branch: str, **_: object
) -> None:
    # The implementer's provisioned PR + branch, mirrored onto the work item —
    # the read side (board links, webhook routing by repo+PR) keys off them.
    subject.update(pr_number=pr_number, branch=branch)


@subscribe(WorkflowEvent.RUNNING, workflow=Build)
async def build_start_marks_ticket_in_progress(*, subject: WorkItem, **_: object) -> None:
    # Every (re)start and gate-resume of a build means the ticket is in progress —
    # including the return from a rework loop that had parked it In Review.
    await subject.set_ticket_status(TicketStatus.IN_PROGRESS)


@subscribe(WorkflowEvent.PARKED, workflow=Build, gate=ReviewWork)
async def review_park_marks_ticket_in_review(*, subject: WorkItem, **_: object) -> None:
    await subject.set_ticket_status(TicketStatus.IN_REVIEW)


@subscribe("repo.pushed", to_default_branch=True)
async def policy_push_reprofiles_the_repo(*, repo: str, paths: list, **_: object) -> None:
    # The operator edited the repo's build policy — re-apply it over the
    # profiled baseline.
    if ".druks/ship/config.yml" in paths:
        project_repo = ProjectRepo.get_for_repo(repo)

        if project_repo:
            await Profile.dispatch(project_repo, refresh_only=True)


@subscribe("pr.review_submitted")
async def pr_review_answers_the_gate(*, repo: str, pr_number: int, payload: dict) -> None:
    item = WorkItem.get_for_pr(repo=repo, pr_number=pr_number, branch=payload["branch"])
    if not item:
        return
    status = item.get_status(workflow=Build)
    if status.is_parked and status.gate == ReviewWork.name:
        await ReviewWork.answer(
            item,
            action=payload["action"],
            reviewer=payload["reviewer"],
            body=payload["body"],
        )


@subscribe("pr.closed")
async def pr_close_settles_the_item(*, repo: str, pr_number: int, payload: dict) -> None:
    """GitHub announcing the verdict on a PR druks owns — one path for every merge,
    druks's own included. A stored verdict makes a redelivery a no-op."""
    item = WorkItem.get_for_pr(repo=repo, pr_number=pr_number, branch=payload["branch"])
    if item and not item.resolution:
        item.resolve(merged=payload["merged"], at=payload["resolved_at"])
        if payload["merged"]:
            await item.ship()
        else:
            await item.close_external()


@subscribe("ticket.transitioned")
async def ticket_transition_drives_the_funnel(*, payload: dict) -> None:
    """Dispatch a build when a ticket from the chosen tracker enters its trigger status."""
    settings = Ship.settings()
    if payload["source"] == settings.tracker and payload["status"] == settings.trigger_status:
        await Build.dispatch(ticket=payload)
