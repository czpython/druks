import logging
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from druks.contrib.ship.policy import RepoPolicy
from druks.contrib.ship.schemas import ProjectRepoSummary, WorkItemSummary
from druks.contrib.ship.ticketing.enums import TicketStatus
from druks.core.apis.github import get_github_client
from druks.db import Base, StoredSubject, db_session
from druks.settings import load_settings
from druks.workflows import FatalError

logger = logging.getLogger(__name__)

# WorkItem.update() sentinel: a field left at _KEEP is untouched, while passing
# None clears the (nullable) column — the two an intent flag has to tell apart.
_KEEP: Any = object()


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    repos: Mapped[list["ProjectRepo"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @classmethod
    def create(cls, *, name: str) -> "Project":
        session = db_session()
        project = cls(name=name)
        session.add(project)
        session.flush()
        return project

    @classmethod
    def get(cls, project_id: int) -> "Project | None":
        return db_session().get(cls, project_id)

    @classmethod
    def get_for_repo(cls, full_name: str) -> "Project | None":
        """Lookup the Project that owns ``full_name`` (e.g. ``clawhaven/acme-app``).

        Returns None when the repo isn't bound to any project yet — the
        caller decides whether to auto-create one or fail.
        """
        stmt = (
            select(cls)
            .join(ProjectRepo, ProjectRepo.project_id == cls.id)
            .where(func.lower(ProjectRepo.full_name) == full_name.lower())
            .limit(1)
        )
        return db_session().scalars(stmt).first()


class ProjectRepo(StoredSubject):
    __tablename__ = "project_repos"
    __table_args__ = (Index("project_repos_project_idx", "project_id"),)

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
    )
    full_name: Mapped[str] = mapped_column(unique=True)
    # Optional free-form role for the dashboard: "design", "infra",
    # "extension". None when the operator hasn't labelled it.
    purpose: Mapped[str | None]
    profile: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    project: Mapped[Project] = relationship(back_populates="repos")

    @classmethod
    def create(
        cls,
        *,
        project_id: int,
        full_name: str,
        purpose: str | None = None,
    ) -> "ProjectRepo":
        session = db_session()
        row = cls(project_id=project_id, full_name=full_name, purpose=purpose)
        session.add(row)
        session.flush()
        return row

    @classmethod
    def get(cls, repo_id: int) -> "ProjectRepo | None":
        return db_session().get(cls, repo_id)

    @classmethod
    def get_in_project(cls, *, project_id: int, repo_id: int) -> "ProjectRepo | None":
        # Scoped lookup for the nested /projects/{project_id}/repos/{repo_id} routes:
        # a repo reached through the wrong project's URL is a miss, not a hit to reject.
        stmt = select(cls).where(cls.id == repo_id, cls.project_id == project_id).limit(1)
        return db_session().scalars(stmt).first()

    def get_label(self) -> str:
        return self.full_name

    def get_summary(self) -> "ProjectRepoSummary":
        return ProjectRepoSummary.model_validate(self)

    @classmethod
    def list_summaries(cls) -> list["ProjectRepoSummary"]:
        # A repo is registered, not transient, so the board is all of them by name.
        stmt = select(cls).order_by(cls.full_name)
        return [repo.get_summary() for repo in db_session().scalars(stmt)]

    def siblings(self) -> list["ProjectRepo"]:
        return [repo for repo in self.project.repos if repo.full_name != self.full_name]

    @property
    def effective_profile(self) -> dict[str, Any]:
        # {} until the repo profiler has run — an unprofiled repo is a normal state.
        return self.profile.get("effective") or {}

    def set_profile(self, *, baseline: dict[str, Any], effective: dict[str, Any]) -> None:
        self.profile = {"baseline": baseline, "effective": effective}
        db_session().flush()

    @classmethod
    def get_for_name(cls, name: str) -> "ProjectRepo | None":
        """Match a ticket signal against the bare repo name.

        Convention: a tracker project name (Linear) or a label names the
        target repo's bare name (e.g. ``acme-app`` maps to
        ``clawhaven/acme-app``). The match is case-insensitive on the
        slug after the last ``/``.
        """
        target = (name or "").strip().lower()
        if not target:
            return
        # SQLite-friendly bare-name suffix match.
        stmt = select(cls).where(func.lower(cls.full_name).like(f"%/{target}")).limit(1)
        return db_session().scalars(stmt).first()

    @classmethod
    def get_for_repo(
        cls, full_name: str, *, raise_on_missing: bool = False
    ) -> "ProjectRepo | None":
        stmt = select(cls).where(func.lower(cls.full_name) == full_name.lower()).limit(1)
        repo = db_session().scalars(stmt).first()
        if raise_on_missing and not repo:
            # A run's stored repo name can outlive its registration — a rename or
            # a GitHub transfer leaves the old full_name behind, and this matches
            # the literal string. Fail with the reason, not an opaque NoneType.
            raise FatalError(
                f"{full_name!r} is not a registered project repo — it may have been "
                "renamed or transferred; re-register it under its current name"
            )
        return repo

    @classmethod
    def lookup(
        cls,
        *,
        project_name: str | None,
        labels: list[str],
    ) -> "ProjectRepo | None":
        """Look up the PR-target repo from a ticket's routing signals.

        Precedence: tracker project name (the original Linear convention),
        then labels — first bare-name match wins. One Jira project can span
        many repos, so a per-ticket label carries the routing
        the project name can't.
        """
        for name in (project_name, *labels):
            if name:
                row = cls.get_for_name(name)
                if row:
                    return row
        return


class WorkItem(StoredSubject):
    __tablename__ = "work_items"
    __table_args__ = (
        Index("work_items_repo_idx", "repo", "pr_number"),
        # One WorkItem per (source, ticket_key) — one row per ticket in the remote
        # tracker. ``source`` is part of the key so Linear "ABC-1" and Jira "ABC-1"
        # don't collide once we support multiple providers.
        Index("work_items_ticket_unique", "source", "ticket_key", unique=True),
        Index("work_items_project_idx", "project_id"),
        Index("work_items_resolved_idx", "resolved_at"),
    )

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id"),
    )
    project: Mapped[Project] = relationship(lazy="joined")
    # Which remote tracker the ticket lives in: ``linear`` / ``github`` /
    # future ``jira``. Combined with ``ticket_key`` to uniquely identify
    # a ticket.
    source: Mapped[str] = mapped_column(default="github")
    title: Mapped[str] = mapped_column(default="")
    # Human-readable issue key in the source: ``ACME-270`` / ``#42`` /
    # ``JIRA-123``. Every item is born from a ticket, so every item has one;
    # Linear's GraphQL accepts the identifier wherever it accepts the UUID.
    ticket_key: Mapped[str]
    ticket_url: Mapped[str | None]
    # The PR-target repo. Still on WorkItem (not derived from project)
    # because a Project can hold N repos but every WorkItem PRs into one.
    repo: Mapped[str]
    pr_number: Mapped[int | None]
    branch: Mapped[str | None]
    # PR outcomes use GitHub's "merged" or "closed" verdict; operator cancellation
    # uses druks's "closed" verdict. Unset while the work is still druks's.
    resolution: Mapped[str | None] = mapped_column(default=None)
    # PR outcomes use GitHub's verdict time; operator cancellation uses druks's
    # cancellation reaction time.
    resolved_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    def get_label(self) -> str:
        return self.ticket_key

    def get_summary(self) -> WorkItemSummary:
        return WorkItemSummary.model_validate(self)

    @classmethod
    def list_summaries(cls) -> list[WorkItemSummary]:
        # Where a run stands colours the row; it never decides whether the row is
        # here. The 500 most-recent cover it; paginate if a board outgrows it.
        stmt = (
            select(cls).where(cls.resolution.is_(None)).order_by(cls.updated_at.desc()).limit(500)
        )
        return [item.get_summary() for item in db_session().scalars(stmt)]

    @classmethod
    def create(
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
        session.flush()
        return item

    @classmethod
    def get(cls, work_item_id: int) -> "WorkItem | None":
        return db_session().get(cls, work_item_id)

    @classmethod
    def get_for_pr(
        cls, *, repo: str, pr_number: int | None, branch: str | None = None
    ) -> "WorkItem | None":
        """The item a pull request belongs to. Its number identifies it; the head
        branch is the fallback for an event that lands before druks mirrored one."""
        if pr_number:
            stmt = (
                select(cls)
                .where(func.lower(cls.repo) == repo.lower(), cls.pr_number == pr_number)
                .order_by(cls.updated_at.desc())
                .limit(1)
            )
            found = db_session().scalars(stmt).first()
            if found:
                return found
        return cls.get_for_branch(repo=repo, branch=branch) if branch else None

    @classmethod
    def get_for_branch(cls, *, repo: str, branch: str) -> "WorkItem | None":
        stmt = (
            select(cls)
            .where(func.lower(cls.repo) == repo.lower(), cls.branch == branch)
            .order_by(cls.updated_at.desc())
            .limit(1)
        )
        return db_session().scalars(stmt).first()

    def start_attempt(self) -> None:
        self.branch = None
        self.pr_number = None
        self.resolution = None
        self.resolved_at = None
        self.updated_at = Base.utc_now()
        db_session().flush()

    def resolve(self, *, merged: bool, at: datetime) -> None:
        # cycle: the extension imports this module at file scope.
        import druks.contrib.ship.extension as ship_extension

        self.resolution = "merged" if merged else "closed"
        self.resolved_at = at
        self.updated_at = Base.utc_now()
        ship_extension.Ship.record_event(type=self.resolution, subject=self)
        db_session().flush()

    async def ship(self) -> None:
        # A build parked on the operator's review is stranded by their merge; a running
        # one converges on its own, its merge step finding the PR already closed.
        from druks.contrib.ship.workflows import Build

        build = self.get_status(workflow=Build)
        if build.is_parked:
            await Build.cancel(self, failure="pr merged while parked")
        await self.set_ticket_status(TicketStatus.DONE)

    async def close_external(self) -> None:
        # The attempt was abandoned, not the ticket, so the ticket returns to the
        # provider's resting pool. Branch cleanup is best-effort: a fetch failure
        # must not strand it there.
        from druks.contrib.ship.workflows import Build

        await Build.cancel(self, failure="pr closed without merge")
        db_session().flush()
        try:
            if (await RepoPolicy.resolve(self.repo)).delete_branch:
                await get_github_client(load_settings()).delete_branch(self.repo, self.branch)
        except Exception:  # noqa: BLE001 — cleanup only
            logger.warning("Skipped branch cleanup for %s.", self.repo, exc_info=True)
        await self.set_ticket_status(TicketStatus.READY_FOR_AGENT)

    @classmethod
    def get_for_ticket_key(
        cls,
        *,
        source: str,
        ticket_key: str,
    ) -> "WorkItem | None":
        """The item a ticket names in its tracker — (source, ticket_key) is the
        row's identity."""
        stmt = select(cls).where(cls.source == source, cls.ticket_key == ticket_key).limit(1)
        return db_session().scalars(stmt).first()

    @classmethod
    def list_recent(cls, *, limit: int = 50, offset: int = 0) -> list["WorkItem"]:
        stmt = select(cls).order_by(cls.updated_at.desc()).limit(limit).offset(offset)
        return list(db_session().scalars(stmt))

    @classmethod
    def list_handoff(cls, *, limit: int = 10) -> list["WorkItem"]:
        stmt = (
            select(cls)
            .where(cls.resolved_at.is_not(None))
            .order_by(cls.resolved_at.desc())
            .limit(limit)
        )
        return list(db_session().scalars(stmt))

    async def set_ticket_status(self, status: TicketStatus) -> None:
        # Lazy: the Ship extension imports this module, so it can't be imported at top.
        import druks.contrib.ship.extension as ship_extension

        tracker = ship_extension.Ship.tracker(self.source)
        # No tracker means nothing to sync (github, or credentials not set yet).
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

    def update(
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
        db_session().flush()
