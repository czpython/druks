from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi import status as http_status

from druks.accounts.dependencies import current_account
from druks.accounts.models import Account
from druks.contrib.software_factory.issues.enums import Priority, Status
from druks.contrib.software_factory.issues.schemas import CommentRead, TicketDetail, TicketEdit
from druks.contrib.software_factory.models import Comment, ProjectRepo, Ticket
from druks.db import Base, db_session

router = APIRouter()


def required_text(value: str, field: str) -> str:
    if text := value.strip():
        return text
    raise HTTPException(http_status.HTTP_422_UNPROCESSABLE_CONTENT, f"{field} must not be blank")


async def require_ticket(identifier: str) -> Ticket:
    if ticket := await Ticket.get_for_identifier(identifier):
        return ticket
    raise HTTPException(http_status.HTTP_404_NOT_FOUND, f"no ticket {identifier!r}")


async def require_repo(repo_id: int) -> ProjectRepo:
    if repo := await ProjectRepo.get(repo_id):
        return repo
    raise HTTPException(http_status.HTTP_404_NOT_FOUND, f"no repo {repo_id}")


async def require_owner(owner_id: str) -> None:
    # The FK is RESTRICT, so a bad id would be a 500 on write.
    if await Account.get(owner_id):
        return
    raise HTTPException(http_status.HTTP_404_NOT_FOUND, f"no account {owner_id!r}")


@router.post(
    "/tickets",
    response_model=TicketDetail,
    response_model_by_alias=True,
    status_code=http_status.HTTP_201_CREATED,
    operation_id="create_ticket",
    tags=["agent"],
)
async def create_ticket(
    title: str = Body(..., embed=True, max_length=200),
    repo_id: int = Body(
        ..., embed=True, description="the GitHub repo this ticket's PR will target"
    ),
    description: str = Body("", embed=True),
    status: Status = Body(Status.BACKLOG, embed=True),
    priority: Priority = Body(Priority.NONE, embed=True),
    owner_id: str | None = Body(None, embed=True),
    account: Account = Depends(current_account),
) -> Ticket:
    """Create a ticket; creating it in Ready for Agent opens a build."""
    title = required_text(title, "title")
    # The owner select submits "" for nobody.
    owner_id = owner_id or None
    if owner_id:
        await require_owner(owner_id)
    return await Ticket.create(
        repo=await require_repo(repo_id),
        title=title,
        description=description,
        status=status,
        priority=priority,
        owner_id=owner_id,
        creator_id=account.id,
    )


@router.patch(
    "/tickets/{identifier}",
    response_model=TicketDetail,
    response_model_by_alias=True,
    operation_id="update_ticket",
    tags=["agent"],
)
async def update_ticket(identifier: str, edit: TicketEdit) -> Ticket:
    """Edit a ticket's title, description, priority, owner, or repo."""
    ticket = await require_ticket(identifier)
    # The owner select submits "" for nobody.
    owner_id = edit.owner_id or None
    if owner_id:
        await require_owner(owner_id)
    if edit.repo_id:
        ticket.repo = await require_repo(edit.repo_id)
    if edit.title is not None:
        ticket.title = required_text(edit.title, "title")
    if edit.description is not None:
        ticket.description = edit.description
    if edit.priority:
        ticket.priority = edit.priority
    # A null owner clears it, so only an omitted owner_id keeps it.
    if "owner_id" in edit.model_fields_set:
        ticket.owner_id = owner_id
    ticket.updated_at = Base.utc_now()
    await db_session().flush()
    return ticket


@router.post(
    "/tickets/{identifier}/status",
    response_model=TicketDetail,
    response_model_by_alias=True,
    operation_id="set_status",
    tags=["agent"],
)
async def set_status(identifier: str, status: Status = Body(..., embed=True)) -> Ticket:
    """Move a ticket to a status; Ready for Agent opens a build."""
    ticket = await require_ticket(identifier)
    await ticket.transition(status)
    return ticket


@router.post(
    "/tickets/{identifier}/comments",
    response_model=CommentRead,
    response_model_by_alias=True,
    status_code=http_status.HTTP_201_CREATED,
    operation_id="add_comment",
    tags=["agent"],
)
async def add_comment(
    identifier: str,
    body: str = Body(..., embed=True),
    account: Account = Depends(current_account),
) -> Comment:
    """Append a comment to a ticket as the calling account."""
    body = required_text(body, "body")
    ticket = await require_ticket(identifier)
    return await ticket.add_comment(author=account, body=body)


@router.get(
    "/tickets/{identifier}",
    response_model=TicketDetail,
    response_model_by_alias=True,
    operation_id="get_ticket",
    tags=["agent"],
)
async def get_ticket(identifier: str) -> Ticket:
    """Read a Druks board ticket such as BOX-3 with its comments; it is not a GitHub issue."""
    return await require_ticket(identifier)
