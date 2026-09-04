from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi import status as http_status
from sqlalchemy import select

from druks.accounts.dependencies import current_account
from druks.accounts.models import Account
from druks.contrib.issues.app import Issues
from druks.contrib.issues.enums import Priority, Status
from druks.contrib.issues.exceptions import InvalidPrefix
from druks.contrib.issues.models import Project, Ticket
from druks.contrib.issues.schemas import CommentRead, ProjectRead, TicketDetail, TicketEdit
from druks.db import Base, db_session
from druks.signals import publish

# The operations own the facts: pages call these doors, and so do the dashboard
# and the sandbox — the same doors, joined to druks ``/mcp`` as ``issues_*``.
# The platform's free subject read-side owns the bare subject-type segment
# (/ticket), so this app's own doors live beside it under /tickets. Reads are
# not doors — pages read the models directly — with one exception: a caller
# that cannot open the page still has to read the ticket it is answering.
#
# ``status`` is a field on two of these doors, so the HTTP codes come in under
# their own name.
router = APIRouter()


def required_text(value: str, field: str) -> str:
    """Trimmed, or a refusal a form can show — whitespace is not content."""
    text = value.strip()
    if not text:
        raise HTTPException(
            http_status.HTTP_422_UNPROCESSABLE_CONTENT, f"{field} must not be blank"
        )
    return text


async def require_ticket(identifier: str) -> Ticket:
    ticket = await Ticket.get_for_identifier(identifier)
    if not ticket:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, f"no ticket {identifier!r}")
    return ticket


async def require_assignee(assignee_id: str) -> None:
    """A ticket is assigned to a real, non-system account or to nobody. The
    assignee FK is RESTRICT, so a bad id would surface as a 500 IntegrityError
    on write — check it here instead, where the answer is a 404 the form can
    show. The system account is druks' own actor, never someone to hand work to."""
    if not await Account.get(assignee_id, exclude_system=True):
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, f"no account {assignee_id!r}")


async def ticket_detail(ticket: Ticket) -> TicketDetail:
    """The ticket and its thread — ``Comment.list_for_ticket`` reads oldest
    first, the order a conversation happened in."""
    comments = await ticket.list_comments()
    # One SELECT for the whole thread's authors rather than one per line:
    # Account has no batch door of its own, so the read is spelled here.
    # Never a 5xx on a gone account either — an id the query answers for is
    # named, and one it does not stays out of the map, so that line reads
    # unattributed.
    author_ids = {comment.author_id for comment in comments}
    authors: dict[str, str] = {}
    if author_ids:
        rows = await db_session().scalars(select(Account).where(Account.id.in_(author_ids)))
        authors = {account.id: account.username for account in rows}
    return TicketDetail(
        identifier=ticket.identifier,
        title=ticket.title,
        description=ticket.description,
        status=Status(ticket.status),
        priority=Priority(ticket.priority),
        project_id=ticket.project_id,
        assignee_id=ticket.assignee_id,
        comments=[
            CommentRead(
                id=comment.id,
                author=authors.get(comment.author_id),
                body=comment.body,
                created_at=comment.created_at,
            )
            for comment in comments
        ],
    )


@router.post(
    "/projects",
    status_code=http_status.HTTP_201_CREATED,
    operation_id="issues_create_project",
    tags=["agent"],
)
async def create_project(
    name: str = Body(..., embed=True, max_length=140),
    prefix: str = Body(
        ...,
        embed=True,
        description="2-6 letters, A-Z — the first half of every identifier this project mints",
    ),
) -> ProjectRead:
    """Open a namespace: a project names its tickets ``{prefix}-1``,
    ``{prefix}-2``, and so on. The prefix is fixed once a number has been
    handed out, so pick the one the team already says out loud."""
    name = required_text(name, "name")
    try:
        # The model's own @validates uppercases and shapes the prefix; a
        # namespace nobody could spell is the caller's mistake, not a 500.
        project = await Project.create(name=name, prefix=prefix)
    except InvalidPrefix as error:
        raise HTTPException(http_status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    return ProjectRead.model_validate(project)


@router.post(
    "/tickets",
    status_code=http_status.HTTP_201_CREATED,
    operation_id="issues_create_ticket",
    tags=["agent"],
)
async def create_ticket(
    title: str = Body(..., embed=True, max_length=200),
    project_id: int = Body(..., embed=True, description="the namespace to mint from"),
    description: str = Body("", embed=True),
    status: Status = Body(Status.TODO, embed=True),
    priority: Priority = Body(Priority.NONE, embed=True),
    assignee_id: str | None = Body(None, embed=True),
) -> TicketDetail:
    """Write a ticket down. It lands in Todo and takes the next number in its
    project's sequence. Creating is quiet: moving a ticket into Ready for Agent
    is what opens a build, so a new ticket publishes nothing."""
    title = required_text(title, "title")
    if not await Project.get(project_id):
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, f"no project {project_id}")
    if assignee_id is not None:
        await require_assignee(assignee_id)
    ticket = await Ticket.create(
        project_id=project_id,
        title=title,
        description=description,
        status=status,
        priority=priority,
        assignee_id=assignee_id,
    )
    return await ticket_detail(ticket)


@router.patch("/tickets/{identifier}", operation_id="issues_update_ticket", tags=["agent"])
async def update_ticket(identifier: str, edit: TicketEdit) -> TicketDetail:
    """Edit what a ticket says — title, description, priority, assignee. What
    you leave out stays as it was, and a title cannot be edited away. Status is
    not here: a title edit is not a state transition, and ``set_status`` is the
    one door that moves a ticket."""
    ticket = await require_ticket(identifier)
    if edit.assignee_id is not None:
        await require_assignee(edit.assignee_id)

    if edit.title is not None:
        ticket.title = required_text(edit.title, "title")
    if edit.description is not None:
        ticket.description = edit.description
    if edit.title is not None or edit.description is not None:
        # Ticket carries setters for priority and assignee but not for its
        # content; stamp and flush the way those setters do.
        ticket.updated_at = Base.utc_now()
        await db_session().flush()
    if edit.priority is not None:
        await ticket.set_priority(edit.priority)
    # A null assignee_id means "unassign", so this field reads the caller's
    # set of fields rather than the value: omitted keeps whoever holds it.
    if "assignee_id" in edit.model_fields_set:
        await ticket.assign(edit.assignee_id)
    return await ticket_detail(ticket)


@router.post("/tickets/{identifier}/status", operation_id="issues_set_status", tags=["agent"])
async def set_status(
    identifier: str,
    status: Status = Body(..., embed=True),
) -> TicketDetail:
    """Move a ticket. This is the only door that moves one, and the only one
    that publishes ``ticket.transitioned`` — Software Factory's funnel reads
    that signal, so a move into the trigger status is what opens a build."""
    ticket = await require_ticket(identifier)
    if ticket.status == status:
        # Already there: a repeat is not a transition, and re-firing would
        # dispatch a second build for one move.
        return await ticket_detail(ticket)

    await ticket.set_status(status)
    project = await Project.get(ticket.project_id)
    assignee = await Account.get(ticket.assignee_id) if ticket.assignee_id else None
    await publish(
        "ticket.transitioned",
        payload={
            "source": "issues",
            "identifier": ticket.identifier,
            # The display label, the way Linear and Jira publish their state
            # names: the funnel's trigger status is spelled as a human reads it.
            "status": status.label,
            "title": ticket.title,
            # The shell serves an app's pages under the app's own name, so this
            # is the path a link in a build or a notification opens.
            "url": f"/{Issues.name}/tickets/{ticket.identifier}",
            "project_name": project.name if project else None,
            "labels": [],
            # An account is a username and nothing else — no display name to
            # tell apart from the address, so both keys carry the one name.
            "assignee_email": assignee.username if assignee else None,
            "assignee_name": assignee.username if assignee else None,
            "completed": status.completed,
            "terminal": status.terminal,
        },
    )
    return await ticket_detail(ticket)


@router.post(
    "/tickets/{identifier}/comments",
    status_code=http_status.HTTP_201_CREATED,
    operation_id="issues_add_comment",
    tags=["agent"],
)
async def add_comment(
    identifier: str,
    body: str = Body(..., embed=True),
    account: Account = Depends(current_account),
) -> CommentRead:
    """Say something on a ticket's thread. The author is you — the signed-in
    account, or the account behind the token — never a field the caller picks.
    Append-only: a thread is a record, so there is no edit and no delete."""
    body = required_text(body, "body")
    ticket = await require_ticket(identifier)
    comment = await ticket.add_comment(author_id=account.id, body=body)
    return CommentRead(
        id=comment.id,
        author=account.username,
        body=comment.body,
        created_at=comment.created_at,
    )


@router.get("/tickets/{identifier}", operation_id="issues_get_ticket", tags=["agent"])
async def get_ticket(identifier: str) -> TicketDetail:
    """Read one ticket: what it asks for, and everything said about it so far,
    oldest comment first."""
    return await ticket_detail(await require_ticket(identifier))
