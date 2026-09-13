import logging
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import ForeignKey, Index, String, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship, selectinload

from druks.accounts.models import Account
from druks.contrib.software_factory.enums import Priority, Status
from druks.contrib.software_factory.exceptions import PrefixTakenError
from druks.contrib.software_factory.policy import RepoPolicy
from druks.contrib.software_factory.schemas import ProjectRepoSummary, WorkItemSummary
from druks.contrib.software_factory.ticketing.enums import TicketStatus
from druks.core.apis.github import get_github_client
from druks.db import Base, StoredSubject, db_session
from druks.signals import publish
from druks.workflows import FatalError

logger = logging.getLogger(__name__)


def derive_prefix(name: str, taken: set[str]) -> str:
    """The name's first two letters and one later letter, the first such prefix
    not in use. A two-letter name is its own prefix."""
    letters = "".join(char for char in name.upper() if char.isascii() and char.isalpha())
    for candidate in [letters[:2] + extra for extra in letters[2:]] or [letters]:
        if len(candidate) >= 2 and candidate not in taken:
            return candidate
    raise PrefixTakenError(name)


# The WorkItem.update() sentinel. A field left at _KEEP stays unchanged, and None
# clears a nullable column.
_KEEP: Any = object()


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    # The ticket identifier namespace, like Linear's team key.
    prefix: Mapped[str] = mapped_column(String(6), unique=True)
    # Bumped inside the UPDATE that mints an identifier and never decremented,
    # so a deleted ticket's number is never handed out again.
    ticket_seq: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    repos: Mapped[list["ProjectRepo"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @classmethod
    async def create(cls, *, name: str) -> "Project":
        session = db_session()
        taken = set(await session.scalars(select(cls.prefix)))
        # repos=[] loads the collection empty. A summary read after flush needs no query.
        project = cls(name=name, prefix=derive_prefix(name, taken), repos=[])
        session.add(project)
        await session.flush()
        return project

    @classmethod
    async def get(cls, project_id: int) -> "Project | None":
        return await db_session().get(cls, project_id)

    @classmethod
    async def get_for_repo(cls, full_name: str) -> "Project | None":
        """The project that owns ``full_name``, for example ``clawhaven/acme-app``."""
        stmt = (
            select(cls)
            .join(ProjectRepo, ProjectRepo.project_id == cls.id)
            .where(func.lower(ProjectRepo.full_name) == full_name.lower())
            .limit(1)
        )
        return (await db_session().scalars(stmt)).first()

    @classmethod
    async def list(cls) -> list["Project"]:
        return list(await db_session().scalars(select(cls).order_by(cls.name)))

    @classmethod
    async def mint_identifier(cls, project_id: int) -> str:
        """The next number in the project's sequence as ``PREFIX-n``. One UPDATE
        ... RETURNING, so concurrent creates queue instead of colliding."""
        statement = (
            sa.update(cls)
            .where(cls.id == project_id)
            .values(ticket_seq=cls.ticket_seq + 1)
            .returning(cls.prefix, cls.ticket_seq)
            .execution_options(synchronize_session=False)
        )
        prefix, number = (await db_session().execute(statement)).one()
        return f"{prefix}-{number}"


class ProjectRepo(StoredSubject):
    __tablename__ = "project_repos"
    __table_args__ = (Index("project_repos_project_idx", "project_id"),)

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
    )
    full_name: Mapped[str] = mapped_column(unique=True)
    # A free-form role for the dashboard, for example "design", "infra", or "app".
    purpose: Mapped[str | None]
    profile: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    project: Mapped[Project] = relationship(back_populates="repos", lazy="joined")

    @classmethod
    async def create(
        cls,
        *,
        project_id: int,
        full_name: str,
        purpose: str | None = None,
    ) -> "ProjectRepo":
        session = db_session()
        row = cls(project_id=project_id, full_name=full_name, purpose=purpose)
        session.add(row)
        await session.flush()
        return row

    @classmethod
    async def get(cls, repo_id: int) -> "ProjectRepo | None":
        return await db_session().get(cls, repo_id)

    @classmethod
    async def list_all(cls) -> list["ProjectRepo"]:
        statement = select(cls).join(Project).order_by(Project.name, cls.full_name)
        return list(await db_session().scalars(statement))

    @classmethod
    async def get_in_project(cls, *, project_id: int, repo_id: int) -> "ProjectRepo | None":
        # For the nested /projects/{project_id}/repos/{repo_id} routes. A repo under the
        # wrong project is a miss, not a hit to reject.
        stmt = select(cls).where(cls.id == repo_id, cls.project_id == project_id).limit(1)
        return (await db_session().scalars(stmt)).first()

    def get_label(self) -> str:
        return self.full_name

    def get_summary(self) -> "ProjectRepoSummary":
        return ProjectRepoSummary.model_validate(self)

    @classmethod
    async def list_summaries(cls, account_id: str | None) -> list["ProjectRepoSummary"]:
        # A repo is registered, not transient, so the board shows all of them by name.
        stmt = select(cls).order_by(cls.full_name)
        return [project_repo.get_summary() for project_repo in await db_session().scalars(stmt)]

    async def siblings(self) -> list["ProjectRepo"]:
        # A fresh query, not ``self.project.repos``: the loaded collection goes
        # stale when a repo is registered through its FK in the same session.
        stmt = select(ProjectRepo).where(
            ProjectRepo.project_id == self.project_id,
            ProjectRepo.full_name != self.full_name,
        )
        return list(await db_session().scalars(stmt))

    @property
    def effective_profile(self) -> dict[str, Any]:
        # The profile is {} until the repo profiler runs. That is a normal state.
        return self.profile.get("effective") or {}

    async def set_profile(self, *, baseline: dict[str, Any], effective: dict[str, Any]) -> None:
        self.profile = {"baseline": baseline, "effective": effective}
        await db_session().flush()

    @classmethod
    async def get_for_name(cls, name: str) -> "ProjectRepo | None":
        """Match a ticket signal to a repo. A bare name matches the slug after the last
        ``/`` without regard to case. A full ``owner/name`` matches exactly."""
        target = name.strip().lower()
        if not target:
            return
        if "/" in target:
            return await cls.get_for_repo(target)
        stmt = select(cls).where(func.lower(cls.full_name).like(f"%/{target}")).limit(1)
        return (await db_session().scalars(stmt)).first()

    @classmethod
    async def get_for_repo(
        cls, full_name: str, *, raise_on_missing: bool = False
    ) -> "ProjectRepo | None":
        stmt = select(cls).where(func.lower(cls.full_name) == full_name.lower()).limit(1)
        project_repo = (await db_session().scalars(stmt)).first()
        if raise_on_missing and not project_repo:
            # A rename or a GitHub transfer can leave a run with an old repo name.
            # Fail with that reason, not with an unclear NoneType error.
            raise FatalError(
                f"{full_name!r} is not a registered project repo. If the repo was "
                "renamed or transferred, register it again under its current name."
            )
        return project_repo

    @classmethod
    async def lookup(
        cls,
        *,
        project_name: str | None,
        labels: list[str],
    ) -> "ProjectRepo | None":
        """The PR target repo for the routing signals of a ticket: the project name
        first, then each label. A label routes a ticket when one project spans many repos."""
        for name in (project_name, *labels):
            if name:
                row = await cls.get_for_name(name)
                if row:
                    return row
        return


class WorkItem(StoredSubject):
    __tablename__ = "work_items"
    __table_args__ = (
        Index("work_items_repo_idx", "repo", "pr_number"),
        # One WorkItem per ticket in the remote tracker. ``source`` is part of the key,
        # so Linear "ABC-1" and Jira "ABC-1" do not collide.
        Index("work_items_ticket_unique", "source", "ticket_key", unique=True),
        Index("work_items_project_idx", "project_id"),
        Index("work_items_resolved_idx", "resolved_at"),
    )

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id"),
    )
    project: Mapped[Project] = relationship(lazy="joined")
    # The tracker of the ticket: ``linear``, ``github``, ``jira``, or ``druks``.
    source: Mapped[str] = mapped_column(default="github")
    title: Mapped[str] = mapped_column(default="")
    # The issue key in the source, for example ``ACME-270``, ``#42``, or ``JIRA-123``.
    # The Linear GraphQL API accepts this key wherever it accepts the UUID.
    ticket_key: Mapped[str]
    ticket_url: Mapped[str | None]
    # The PR target repo. A project can hold many repos, but a work item targets one.
    repo: Mapped[str]
    pr_number: Mapped[int | None]
    branch: Mapped[str | None]
    # GitHub sets "merged" or "closed" for a PR outcome. An operator cancel sets
    # "closed". The value is None while the work is open.
    resolution: Mapped[str | None] = mapped_column(default=None)
    # The time of the GitHub verdict, or of the cancel reaction.
    resolved_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    def get_label(self) -> str:
        return self.ticket_key

    def get_summary(self) -> WorkItemSummary:
        return WorkItemSummary.model_validate(self)

    @classmethod
    async def list_summaries(cls, account_id: str | None) -> list[WorkItemSummary]:
        return [item.get_summary() for item in await cls.list_unresolved()]

    @classmethod
    async def list_unresolved(cls) -> list["WorkItem"]:
        # The run state colors a row but never decides whether the row shows. The 500
        # most recent rows cover the board.
        stmt = (
            select(cls).where(cls.resolution.is_(None)).order_by(cls.updated_at.desc()).limit(500)
        )
        return list(await db_session().scalars(stmt))

    @classmethod
    async def create(
        cls,
        *,
        project_id: int,
        source: str = "github",
        title: str,
        ticket_key: str,
        ticket_url: str | None = None,
        repo: str,
    ) -> "WorkItem":
        session = db_session()
        item = cls(
            project_id=project_id,
            source=source,
            title=title,
            ticket_key=ticket_key,
            ticket_url=ticket_url,
            repo=repo,
        )
        session.add(item)
        await session.flush()
        return item

    @classmethod
    async def get(cls, work_item_id: int) -> "WorkItem | None":
        return await db_session().get(cls, work_item_id)

    @classmethod
    async def get_for_pr(
        cls, *, repo: str, pr_number: int | None, branch: str | None = None
    ) -> "WorkItem | None":
        """The item of a pull request, found by its number. The head branch is the
        fallback for an event that arrives before Druks stores the number."""
        if pr_number:
            stmt = (
                select(cls)
                .where(func.lower(cls.repo) == repo.lower(), cls.pr_number == pr_number)
                .order_by(cls.updated_at.desc())
                .limit(1)
            )
            found = (await db_session().scalars(stmt)).first()
            if found:
                return found
        return await cls.get_for_branch(repo=repo, branch=branch) if branch else None

    @classmethod
    async def get_for_branch(cls, *, repo: str, branch: str) -> "WorkItem | None":
        stmt = (
            select(cls)
            .where(func.lower(cls.repo) == repo.lower(), cls.branch == branch)
            .order_by(cls.updated_at.desc())
            .limit(1)
        )
        return (await db_session().scalars(stmt)).first()

    async def start_attempt(self) -> None:
        self.branch = None
        self.pr_number = None
        self.resolution = None
        self.resolved_at = None
        self.updated_at = Base.utc_now()
        await db_session().flush()

    async def resolve(self, *, merged: bool, at: datetime) -> None:
        # Import cycle: the app imports this module through ticketing/druks.py.
        import druks.contrib.software_factory.app as software_factory_app

        self.resolution = "merged" if merged else "closed"
        self.resolved_at = at
        self.updated_at = Base.utc_now()
        await software_factory_app.SoftwareFactory.record_event(type=self.resolution, subject=self)
        await db_session().flush()

    async def ship(self) -> None:
        # A merge strands a build that is parked on review, so cancel it. A running build
        # finishes by itself, because its merge step finds the PR closed.
        from druks.contrib.software_factory.workflows import Build

        build = await self.get_status(workflow=Build)
        if build.is_parked:
            await Build.cancel(self, failure="pr merged while parked")
        await self.set_ticket_status(TicketStatus.DONE)

    async def close_external(self) -> None:
        # The attempt stopped, not the ticket, so the ticket goes back to its resting
        # status. Branch cleanup is best effort, and a failure must not block that move.
        from druks.contrib.software_factory.workflows import Build

        await Build.cancel(self, failure="pr closed without merge")
        await db_session().flush()
        try:
            if (await RepoPolicy.resolve(self.repo)).delete_branch:
                await (await get_github_client()).delete_branch(self.repo, self.branch)
        except Exception:  # noqa: BLE001 — cleanup only
            logger.warning("Skipped branch cleanup for %s.", self.repo, exc_info=True)
        await self.set_ticket_status(TicketStatus.BACKLOG)

    @classmethod
    async def get_for_ticket_key(
        cls,
        *,
        source: str,
        ticket_key: str,
    ) -> "WorkItem | None":
        stmt = select(cls).where(cls.source == source, cls.ticket_key == ticket_key).limit(1)
        return (await db_session().scalars(stmt)).first()

    @classmethod
    async def list_recent(cls, *, limit: int = 50, offset: int = 0) -> list["WorkItem"]:
        stmt = select(cls).order_by(cls.updated_at.desc()).limit(limit).offset(offset)
        return list(await db_session().scalars(stmt))

    @classmethod
    async def list_handoff(cls, *, limit: int = 10) -> list["WorkItem"]:
        stmt = (
            select(cls)
            .where(cls.resolved_at.is_not(None))
            .order_by(cls.resolved_at.desc())
            .limit(limit)
        )
        return list(await db_session().scalars(stmt))

    async def set_ticket_status(self, status: TicketStatus) -> None:
        # Import cycle: the app imports this module through ticketing/druks.py.
        import druks.contrib.software_factory.app as software_factory_app

        tracker = await software_factory_app.SoftwareFactory.get_tracker(self.source)
        # A github item, a source that is no longer selected, or missing credentials
        # has no tracker, so nothing syncs.
        if not tracker:
            return

        async with tracker:
            try:
                await tracker.set_status(self.ticket_key, status)
            except (ValueError, *tracker.known_exceptions):
                logger.warning(
                    "Could not sync %s ticket %s to %s.",
                    self.source,
                    self.ticket_key,
                    status.value,
                    exc_info=True,
                )

    async def update(
        self,
        *,
        title: str = _KEEP,
        ticket_url: str | None = _KEEP,
        pr_number: int | None = _KEEP,
        branch: str | None = _KEEP,
        project_id: int = _KEEP,
    ) -> None:
        if title is not _KEEP:
            self.title = title
        if ticket_url is not _KEEP:
            self.ticket_url = ticket_url
        if pr_number is not _KEEP:
            self.pr_number = pr_number
        if branch is not _KEEP:
            self.branch = branch
        if project_id is not _KEEP:
            self.project_id = project_id
        self.updated_at = Base.utc_now()
        await db_session().flush()


class Comment(Base):
    __tablename__ = "ticket_comments"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"))
    author_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    author: Mapped[Account] = relationship(lazy="joined")
    body: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    identifier: Mapped[str] = mapped_column(unique=True)
    title: Mapped[str]
    description: Mapped[str] = mapped_column(default="")
    # String columns, never PG enums. A new status then needs no ALTER TYPE.
    status: Mapped[str] = mapped_column(default=Status.BACKLOG)
    priority: Mapped[str] = mapped_column(default=Priority.NONE)
    repo_id: Mapped[int] = mapped_column(ForeignKey("project_repos.id", ondelete="CASCADE"))
    project_repo: Mapped[ProjectRepo] = relationship(lazy="joined")
    assignee_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    creator_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    comments: Mapped[list[Comment]] = relationship(order_by=[Comment.created_at, Comment.id])

    @classmethod
    async def create(
        cls,
        *,
        project_repo: ProjectRepo,
        title: str,
        description: str = "",
        status: Status = Status.BACKLOG,
        priority: Priority = Priority.NONE,
        assignee_id: str | None = None,
        creator_id: str | None = None,
    ) -> "Ticket":
        # comments=[] loads the collection empty. A read after flush needs no query.
        ticket = cls(
            identifier=await Project.mint_identifier(project_repo.project_id),
            project_repo=project_repo,
            title=title,
            description=description,
            status=status,
            priority=priority,
            assignee_id=assignee_id,
            creator_id=creator_id,
            comments=[],
        )
        session = db_session()
        session.add(ticket)
        await session.flush()
        # Creating in Ready for Agent is the same trigger as moving into it.
        if status == Status.READY_FOR_AGENT:
            await ticket._emit_transitioned(status)
        return ticket

    @classmethod
    async def get_for_identifier(cls, identifier: str) -> "Ticket | None":
        statement = (
            select(cls).where(cls.identifier == identifier).options(selectinload(cls.comments))
        )
        return (await db_session().scalars(statement)).first()

    @classmethod
    async def list_matching(
        cls,
        *,
        status: Status | None = None,
        priority: Priority | None = None,
        assignee: str = "",
        creator: str = "",
        project_id: int | None = None,
        repo_id: int | None = None,
        updated_since: datetime | None = None,
    ) -> list["Ticket"]:
        statement = select(cls)
        if status:
            statement = statement.where(cls.status == status)
        if priority:
            statement = statement.where(cls.priority == priority)
        if assignee == "none":
            statement = statement.where(cls.assignee_id.is_(None))
        elif assignee:
            statement = statement.where(cls.assignee_id == assignee)
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

    async def save(self) -> None:
        self.updated_at = Base.utc_now()
        await db_session().flush()

    async def transition(self, status: Status) -> None:
        """Write the status and tell the funnel. A repeat is a no-op, so it never
        opens a second build."""
        if self.status != status:
            self.status = status
            await self.save()
            await self._emit_transitioned(status)

    async def _emit_transitioned(self, status: Status) -> None:
        assignee = await Account.get(self.assignee_id) if self.assignee_id else None
        await publish(
            "ticket.transitioned",
            payload={
                "source": "druks",
                "identifier": self.identifier,
                # The display label, the way Linear and Jira publish state names.
                "status": status.label,
                "title": self.title,
                "url": f"/software_factory/tickets/{self.identifier}",
                "project_name": self.project_repo.full_name,
                "labels": [],
                "assignee_id": self.assignee_id,
                "assignee_email": assignee.username if assignee else None,
                "assignee_name": assignee.username if assignee else None,
            },
        )

    async def add_comment(self, *, author: Account, body: str) -> Comment:
        comment = Comment(author=author, body=body)
        self.comments.append(comment)
        await db_session().flush()
        return comment
