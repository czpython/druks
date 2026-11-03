from druks.contrib.software_factory.app import SoftwareFactory
from druks.contrib.software_factory.contracts import ReviewWork
from druks.contrib.software_factory.enums import Resolution, Status
from druks.contrib.software_factory.models import ProjectRepo, WorkItem
from druks.contrib.software_factory.ticketing.enums import TicketStatus
from druks.contrib.software_factory.workflows import Build, Profile
from druks.models import Base
from druks.signals import publish, subscribe
from druks.workflows import WorkflowEvent


@subscribe(WorkflowEvent.SCHEDULED, workflow=Build)
async def new_build_claims_the_item(*, subject: WorkItem, **_: object) -> None:
    await subject.start_attempt()


@subscribe(WorkflowEvent.CANCELLED, workflow=Build)
async def cancelled_build_settles_the_item(*, subject: WorkItem, **_: object) -> None:
    """An operator cancellation explicitly abandons the work item."""
    if not subject.resolution:
        await subject.resolve(Resolution.CANCELLED, at=Base.utc_now())


@subscribe("pr.opened", payload__is_fork=False)
async def pr_open_fills_the_item(*, repo: str, pr_number: int, payload: dict) -> None:
    """GitHub announcing the PR the implementer opened on the branch Build created. A fork
    can use any branch name, so a fork's PR never fills the item. Webhook routing by repo
    and PR keys off the stored number, so a redelivery finds it and stops."""
    item = await WorkItem.get_for_branch(repo=repo, branch=payload["branch"])
    if item and not item.pr_number:
        await item.update(pr_number=pr_number)
        await item.announce("build.pr_opened", repo=item.repo, pr_number=pr_number)


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
    if ".druks/software_factory/config.yml" in paths:
        project_repo = await ProjectRepo.get_for_repo(repo)

        if project_repo:
            await Profile.dispatch(project_repo, refresh_only=True)


@subscribe("pr.review_submitted", payload__author_can_write=True)
async def pr_review_answers_the_gate(*, repo: str, pr_number: int, payload: dict) -> None:
    item = await WorkItem.get_for_pr(repo=repo, pr_number=pr_number)
    if item:
        status = await item.get_status(workflow=Build)
        if status.is_parked and status.gate == ReviewWork.name:
            await ReviewWork.answer(
                item,
                action=payload["action"],
                reviewer=payload["reviewer"],
                note=payload["body"],
            )


@subscribe("pr.closed")
async def pr_close_settles_the_item(*, repo: str, pr_number: int, payload: dict) -> None:
    """GitHub announcing the verdict on a PR druks owns — one path for every merge,
    druks's own included. A stored verdict makes a redelivery a no-op. GitHub's verdict
    replaces an operator's cancel, because the PR can merge after Druks stops."""
    item = await WorkItem.get_for_pr(repo=repo, pr_number=pr_number)
    if item and item.resolution in (None, Resolution.CANCELLED):
        resolution = Resolution.MERGED if payload["merged"] else Resolution.CLOSED
        await item.resolve(resolution, at=payload["resolved_at"])


@subscribe("issue.labeled")
async def issue_label_transitions_the_ticket(*, repo: str, number: int, payload: dict) -> None:
    """GitHub has no status field, so a label is the status of a GitHub ticket."""
    assignee_id = payload["assignee_id"]
    await publish(
        "ticket.transitioned",
        payload={
            "source": "github",
            # Issue numbers repeat across repos, so the key carries the repo.
            "identifier": f"{repo}#{number}",
            "status": payload["label"],
            "title": payload["title"],
            "url": payload["url"],
            # The issue's own repo is the build's repo, so no label routes it elsewhere.
            "project_name": repo,
            "labels": [],
            "actor_id": str(payload["actor_id"]),
            "assignee_id": str(assignee_id) if assignee_id else None,
            "assignee_email": None,
            "assignee_name": payload["assignee"],
        },
    )


@subscribe("ticket.transitioned")
async def ticket_transition_drives_the_funnel(*, payload: dict) -> None:
    """Dispatch a build when a ticket from the chosen tracker enters its trigger status."""
    settings = await SoftwareFactory.settings()
    trigger = settings.trigger_status
    # The board has a fixed trigger. The hidden setting can keep a value of another tracker.
    if settings.tracker == "druks":
        trigger = Status.READY_FOR_AGENT.label
    if payload["source"] == settings.tracker and payload["status"] == trigger:
        await Build.dispatch(ticket=payload)
