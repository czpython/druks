import re
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import ForeignKey, String, select
from sqlalchemy.orm import Mapped, mapped_column, validates

from druks.accounts.models import Account
from druks.contrib.issues.app import Issues
from druks.contrib.issues.enums import Priority, Status
from druks.contrib.issues.exceptions import InvalidPrefix, PrefixLocked, ProjectNotFound
from druks.contrib.issues.schemas import TicketSummary
from druks.db import Base, StoredSubject, db_session
from druks.signals import publish

# A project's prefix is the identifier namespace — Linear's team key. Short
# enough to read at a glance, long enough to stay distinct.
PREFIX_PATTERN = "^[A-Z]{2,6}$"
PREFIX_RE = re.compile(PREFIX_PATTERN)


def normalize_prefix(prefix: str) -> str:
    """The stored form of an operator's prefix: uppercase, and 2-6 letters or
    nothing at all."""
    normalized = prefix.strip().upper()
    if not PREFIX_RE.match(normalized):
        raise InvalidPrefix(prefix)
    return normalized


class Project(Base):
    __tablename__ = "issues_projects"
    # The prefix shape lives in the database too: validation covers this app's
    # own doors, the constraint covers everything else that can write the row.
    __table_args__ = (
        sa.CheckConstraint(f"prefix ~ '{PREFIX_PATTERN}'", name="issues_projects_prefix_shape"),
    )

    # Not a StoredSubject: no run is ever *about* a project — runs are about the
    # tickets it namespaces.
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    prefix: Mapped[str] = mapped_column(String(6), unique=True)
    # The monotonic ticket sequence. It only ever goes up: it is bumped inside
    # the INSERT that mints an identifier and is never decremented, so deleting
    # DRU-1 does not hand DRU-1 out again. Deriving the number from a count or a
    # MAX over the tickets table would do exactly that, and would race besides.
    ticket_seq: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    @validates("prefix")
    def _normalize_prefix(self, key: str, prefix: str) -> str:
        # Every assignment path — create, an edit, a fixture — normalizes and
        # validates, so an unshaped prefix can't reach the column.
        return normalize_prefix(prefix)

    @classmethod
    async def create(cls, *, name: str, prefix: str) -> "Project":
        session = db_session()
        project = cls(name=name, prefix=prefix)
        session.add(project)
        # A duplicate name or prefix surfaces here, as the unique violation it
        # is: the board refuses two namespaces that spell the same thing.
        await session.flush()
        return project

    @classmethod
    async def get(cls, project_id: int) -> "Project | None":
        return await db_session().get(cls, project_id)

    @classmethod
    async def list(cls) -> list["Project"]:
        statement = select(cls).order_by(cls.created_at, cls.id)
        return list(await db_session().scalars(statement))

    @classmethod
    async def mint_identifier(cls, project_id: int) -> str:
        """Take the next number in this project's sequence and spell it as an
        identifier. One statement: the row is locked, bumped, and read in the
        same UPDATE ... RETURNING, so concurrent creates queue instead of
        colliding and no number is ever handed out twice."""
        statement = (
            sa.update(cls)
            .where(cls.id == project_id)
            .values(ticket_seq=cls.ticket_seq + 1)
            .returning(cls.prefix, cls.ticket_seq)
            .execution_options(synchronize_session=False)
        )
        row = (await db_session().execute(statement)).one_or_none()
        if not row:
            raise ProjectNotFound(project_id)
        prefix, number = row
        return f"{prefix}-{number}"

    async def set_prefix(self, prefix: str) -> None:
        """Rename the namespace — refused once a ticket has been minted against
        it, because the identifiers already handed out spell the old prefix and
        are never rewritten. The counter is read from the row rather than the
        instance: ``mint_identifier`` bumps it with an UPDATE this session's
        copy has not seen."""
        session = db_session()
        minted = await session.scalar(select(Project.ticket_seq).where(Project.id == self.id))
        # Normalize explicitly for this comparison: @validates only normalizes
        # on assignment, which happens below, after the lock check.
        if minted and normalize_prefix(prefix) != self.prefix:
            raise PrefixLocked(self.prefix)
        self.prefix = prefix
        await session.flush()


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
    status: Mapped[str] = mapped_column(default=Status.TODO)
    priority: Mapped[str] = mapped_column(default=Priority.NONE)
    # Required: a ticket without a namespace could not be named.
    project_id: Mapped[int] = mapped_column(ForeignKey("issues_projects.id"))
    # Optional: a ticket exists before anyone picks it up.
    assignee_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    @classmethod
    async def create(
        cls,
        *,
        project_id: int,
        title: str,
        description: str = "",
        status: Status = Status.TODO,
        priority: Priority = Priority.NONE,
        assignee_id: str | None = None,
    ) -> "Ticket":
        session = db_session()
        ticket = cls(
            identifier=await Project.mint_identifier(project_id),
            project_id=project_id,
            title=title,
            description=description,
            status=status,
            priority=priority,
            assignee_id=assignee_id,
        )
        session.add(ticket)
        await session.flush()
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
    async def list_board(cls) -> list["Ticket"]:
        """Everything on the board — cancelled tickets are off it. The page
        groups these by status; the model just says which rows are live."""
        statement = (
            select(cls)
            .where(cls.status != Status.CANCELLED)
            .order_by(cls.updated_at.desc(), cls.id.desc())
        )
        return list(await db_session().scalars(statement))

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
        project = await Project.get(self.project_id)
        assignee = await Account.get(self.assignee_id) if self.assignee_id else None
        await publish(
            "ticket.transitioned",
            payload={
                "source": Issues.name,
                "identifier": self.identifier,
                # Display label, the way Linear and Jira publish state names:
                # the funnel's trigger status is spelled as a human reads it.
                "status": status.label,
                "title": self.title,
                "url": f"/{Issues.name}/tickets/{self.identifier}",
                "project_name": project.name if project else None,
                "labels": [],
                "assignee_email": assignee.username if assignee else None,
                "assignee_name": assignee.username if assignee else None,
                "completed": status.completed,
                "terminal": status.terminal,
            },
        )

    async def set_priority(self, priority: Priority) -> None:
        self.priority = priority
        self.updated_at = Base.utc_now()
        await db_session().flush()

    async def assign(self, assignee_id: str | None) -> None:
        self.assignee_id = assignee_id
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
