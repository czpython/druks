from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import ForeignKey, select
from sqlalchemy.orm import Mapped, mapped_column, relationship

from druks.accounts.models import Account
from druks.contrib.software_factory.exceptions import RepoNotFound
from druks.contrib.software_factory.issues.enums import Priority, Status
from druks.contrib.software_factory.issues.schemas import TicketSummary
from druks.contrib.software_factory.models import Project, ProjectRepo
from druks.db import Base, StoredSubject, db_session
from druks.signals import publish


class Ticket(StoredSubject):
    __tablename__ = "issues_tickets"

    # id: the integer subject key inherited from StoredSubject; the class name
    # derives subject_type "ticket".
    identifier: Mapped[str] = mapped_column(unique=True)
    title: Mapped[str]
    description: Mapped[str] = mapped_column(default="")
    # Status and priority are String columns driven by this app's closed
    # StrEnums, not native PG enum types: the workflow stays in code and a label
    # change never needs an ALTER TYPE.
    status: Mapped[str] = mapped_column(default=Status.BACKLOG)
    priority: Mapped[str] = mapped_column(default=Priority.NONE)
    # Required: a ticket names the repo its PR will land in, and the identifier
    # is minted from that repo's project.
    repo_id: Mapped[int] = mapped_column(ForeignKey("project_repos.id"))
    repo: Mapped[ProjectRepo] = relationship(lazy="joined")
    # Optional: a ticket exists before anyone picks it up.
    owner_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), default=None
    )
    # Who opened it. Optional on the row so a ticket minted outside the HTTP
    # door still stores; the create door stamps the signed-in account.
    creator_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    @classmethod
    async def create(
        cls,
        *,
        repo_id: int,
        title: str,
        description: str = "",
        status: Status = Status.BACKLOG,
        priority: Priority = Priority.NONE,
        owner_id: str | None = None,
        creator_id: str | None = None,
    ) -> "Ticket":
        session = db_session()
        repo = await ProjectRepo.get(repo_id)
        if not repo:
            raise RepoNotFound(repo_id)
        ticket = cls(
            identifier=await Project.mint_identifier(repo.project_id),
            repo_id=repo_id,
            title=title,
            description=description,
            status=status,
            priority=priority,
            owner_id=owner_id,
            creator_id=creator_id,
        )
        session.add(ticket)
        await session.flush()
        # Creating already in Ready for Agent is arriving at the trigger, the
        # same as a later move into it. Backlog and the rest stay quiet: drafting
        # is not a funnel event.
        if status == Status.READY_FOR_AGENT:
            await ticket._emit_transitioned(status)
        return ticket

    def get_label(self) -> str:
        # The stable handle, never the mutable title: events snapshot the label
        # and the log should not disagree with itself.
        return self.identifier

    def get_summary(self) -> TicketSummary:
        return TicketSummary.model_validate(self)

    @classmethod
    async def get_for_identifier(cls, identifier: str) -> "Ticket | None":
        statement = select(cls).where(cls.identifier == identifier)
        return (await db_session().scalars(statement)).first()

    @classmethod
    async def list_matching(
        cls,
        *,
        exclude_cancelled: bool = False,
        status: str = "",
        priority: str = "",
        owner: str = "",
        creator: str = "",
        project_id: int | None = None,
        repo_id: int | None = None,
        updated_since: datetime | None = None,
    ) -> list["Ticket"]:
        statement = select(cls)
        if exclude_cancelled:
            statement = statement.where(cls.status.notin_((Status.CANCELLED, Status.BLOCKED)))
        if status:
            statement = statement.where(cls.status == status)
        if priority:
            statement = statement.where(cls.priority == priority)
        if owner == "none":
            statement = statement.where(cls.owner_id.is_(None))
        elif owner:
            statement = statement.where(cls.owner_id == owner)
        if creator:
            statement = statement.where(cls.creator_id == creator)
        if repo_id:
            statement = statement.where(cls.repo_id == repo_id)
        elif project_id:
            statement = statement.where(
                cls.repo_id.in_(select(ProjectRepo.id).where(ProjectRepo.project_id == project_id))
            )
        if updated_since:
            statement = statement.where(cls.updated_at >= updated_since)
        statement = statement.order_by(cls.updated_at.desc(), cls.id.desc())
        return list(await db_session().scalars(statement))

    @classmethod
    async def list_board(cls) -> list["Ticket"]:
        """Everything on the board — cancelled and blocked tickets are off it.
        The page groups these by status; the model just says which rows are live."""
        return await cls.list_matching(exclude_cancelled=True)

    @classmethod
    async def list_for_status(cls, status: Status) -> list["Ticket"]:
        statement = (
            select(cls).where(cls.status == status).order_by(cls.updated_at.desc(), cls.id.desc())
        )
        return list(await db_session().scalars(statement))

    @classmethod
    async def list_summaries(cls, account_id: str | None) -> list[TicketSummary]:
        # One board for the appliance: what a team is working on belongs to
        # everyone reading it, not to whoever happens to be signed in.
        return [ticket.get_summary() for ticket in await cls.list_board()]

    async def set_status(self, status: Status) -> None:
        self.status = status
        self.updated_at = Base.utc_now()
        await db_session().flush()

    async def transition(self, status: Status) -> None:
        """Write a new status and tell the funnel. Already-there is a no-op so
        a repeat cannot dispatch a second build."""
        if self.status == status:
            return
        await self.set_status(status)
        await self._emit_transitioned(status)

    async def _emit_transitioned(self, status: Status) -> None:
        repo = await ProjectRepo.get(self.repo_id)
        owner = await Account.get(self.owner_id) if self.owner_id else None
        await publish(
            "ticket.transitioned",
            payload={
                "source": "issues",
                "identifier": self.identifier,
                # Display label, the way Linear and Jira publish state names:
                # the funnel's trigger status is spelled as a human reads it.
                "status": status.label,
                "title": self.title,
                "url": f"/software_factory/tickets/{self.identifier}",
                # Bare repo name so Build.dispatch / ProjectRepo.lookup still
                # find the PR target the operator picked.
                "project_name": repo.full_name.rsplit("/", 1)[-1],
                "labels": [],
                "assignee_email": owner.username if owner else None,
                "assignee_name": owner.username if owner else None,
                "completed": status.completed,
                "terminal": status.terminal,
            },
        )

    async def set_priority(self, priority: Priority) -> None:
        self.priority = priority
        self.updated_at = Base.utc_now()
        await db_session().flush()

    async def set_owner(self, owner_id: str | None) -> None:
        self.owner_id = owner_id
        self.updated_at = Base.utc_now()
        await db_session().flush()

    async def add_comment(self, *, author_id: str, body: str) -> "Comment":
        return await Comment.create(ticket_id=self.id, author_id=author_id, body=body)

    async def list_comments(self) -> list["Comment"]:
        return await Comment.list_for_ticket(self.id)

    async def delete(self) -> None:
        """Drop the ticket and its thread. The project's counter is untouched —
        a retired number is retired, not recycled."""
        session = db_session()
        await session.execute(sa.delete(Comment).where(Comment.ticket_id == self.id))
        await session.delete(self)
        await session.flush()


class Comment(Base):
    __tablename__ = "issues_comments"

    # A row, not an event and not a StoredSubject: events stay facts about what
    # happened, while a comment is editable content the thread reads back in
    # order. Chat's ``Message`` is the precedent.
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("issues_tickets.id"))
    # The signed-in account that wrote it — required, so every line on a thread
    # has someone's name against it.
    author_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    body: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    @classmethod
    async def create(cls, *, ticket_id: int, author_id: str, body: str) -> "Comment":
        session = db_session()
        comment = cls(ticket_id=ticket_id, author_id=author_id, body=body)
        session.add(comment)
        await session.flush()
        return comment

    @classmethod
    async def list_for_ticket(cls, ticket_id: int) -> list["Comment"]:
        """The thread, oldest first — a conversation reads down. A ticket nobody
        has commented on is an empty list, never None."""
        statement = select(cls).where(cls.ticket_id == ticket_id).order_by(cls.created_at, cls.id)
        return list(await db_session().scalars(statement))
