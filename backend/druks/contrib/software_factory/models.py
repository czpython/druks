import logging
import re
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, ForeignKey, Index, String, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from druks.contrib.software_factory.exceptions import (
    InvalidPrefix,
    MissingPrefix,
    PrefixLocked,
    PrefixTaken,
    ProjectNotFound,
)
from druks.contrib.software_factory.policy import RepoPolicy
from druks.contrib.software_factory.schemas import ProjectRepoSummary, WorkItemSummary
from druks.contrib.software_factory.ticketing.enums import TicketStatus
from druks.core.apis.github import get_github_client
from druks.db import Base, StoredSubject, db_session
from druks.workflows import FatalError

logger = logging.getLogger(__name__)

# A project's prefix is the identifier namespace — Linear's team key. Short
# enough to read at a glance, long enough to stay distinct. Null until set:
# a GitHub project can exist before anyone mints a ticket against it.
# Two to six letters, or the clash walk's two letters plus a digit 1-9.
PREFIX_PATTERN = r"^([A-Z]{2,6}|[A-Z]{2}[1-9])$"
PREFIX_RE = re.compile(PREFIX_PATTERN)
PREFIX_UNIQUE = "projects_prefix_key"


def _raise_prefix_taken(error: IntegrityError, prefix: str | None) -> None:
    constraint = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
    if prefix and constraint == PREFIX_UNIQUE:
        raise PrefixTaken(prefix) from error


def normalize_prefix(prefix: str) -> str:
    """The stored form of an operator's prefix: uppercase, 2-6 letters, or two
    letters and a digit 1-9."""
    normalized = prefix.strip().upper()
    if not PREFIX_RE.match(normalized):
        raise InvalidPrefix(prefix)
    return normalized


def prefix_candidates(name: str) -> list[str]:
    """Prefixes derived from a project name, first suggestion then clash walk.

    Letters only, uppercase. First is positions 1, 2, 3. A clash retries 1, 2
    and the next remaining letter (1+2+4, 1+2+5, …). After those, letters 1+2
    plus a digit 1-9. A name with fewer than two letters has no candidates.
    """
    letters = "".join(ch for ch in name.upper() if ch.isascii() and ch.isalpha())
    if len(letters) < 2:
        return []
    stem = letters[:2]
    out: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        if candidate not in seen:
            seen.add(candidate)
            out.append(candidate)

    if len(letters) >= 3:
        add(letters[:3])
        for extra in letters[3:]:
            add(stem + extra)
    for digit in "123456789":
        add(f"{stem}{digit}")
    return out


# WorkItem.update() sentinel: a field left at _KEEP is untouched, while passing
# None clears the (nullable) column — the two an intent flag has to tell apart.
_KEEP: Any = object()


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint(
            f"(prefix IS NULL) OR (prefix ~ '{PREFIX_PATTERN}')",
            name="projects_prefix_shape",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    prefix: Mapped[str | None] = mapped_column(String(6), unique=True, default=None)
    # The monotonic ticket sequence. It only ever goes up: it is bumped inside
    # the UPDATE that mints an identifier and is never decremented, so deleting
    # DRU-1 does not hand DRU-1 out again. Deriving the number from a count or a
    # MAX over the tickets table would do exactly that, and would race besides.
    ticket_seq: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    repos: Mapped[list["ProjectRepo"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @validates("prefix")
    def _normalize_prefix(self, key: str, prefix: str | None) -> str | None:
        # Every assignment path — create, an edit, a fixture — normalizes and
        # validates, so an unshaped prefix can't reach the column. Blank is
        # unset: a project can exist before it mints tickets.
        if prefix is None or not str(prefix).strip():
            return None
        return normalize_prefix(prefix)

    @classmethod
    async def create(cls, *, name: str, prefix: str | None = None) -> "Project":
        session = db_session()
        submitted = None if prefix is None or not str(prefix).strip() else normalize_prefix(prefix)
        candidates = prefix_candidates(name)
        suggested = candidates[0] if candidates else None
        if submitted is None or submitted == suggested:
            taken = set(await session.scalars(select(cls.prefix).where(cls.prefix.is_not(None))))
            chosen = next((item for item in candidates if item not in taken), None)
            if chosen is None and (submitted is not None or candidates):
                raise PrefixTaken(candidates[-1] if candidates else submitted or "")
        else:
            chosen = submitted
        # Seed the collection as loaded-empty: a fresh project has no repos, and
        # the summary read right after flush must not trigger a lazy load.
        project = cls(name=name, prefix=chosen, repos=[])
        session.add(project)
        try:
            await session.flush()
        except IntegrityError as error:
            _raise_prefix_taken(error, project.prefix)
            raise
        return project

    @classmethod
    async def get(cls, project_id: int) -> "Project | None":
        return await db_session().get(cls, project_id)

    @classmethod
    async def get_for_repo(cls, full_name: str) -> "Project | None":
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
        return (await db_session().scalars(stmt)).first()

    @classmethod
    async def list(cls) -> list["Project"]:
        return list(await db_session().scalars(select(cls).order_by(cls.name)))

    @classmethod
    async def mint_identifier(cls, project_id: int) -> str:
        """Take the next number in this project's sequence and spell it as an
        identifier. One statement: the row is locked, bumped, and read in the
        same UPDATE ... RETURNING, so concurrent creates queue instead of
        colliding and no number is ever handed out twice. A project with no
        prefix is refused rather than minting a nameless ticket."""
        statement = (
            sa.update(cls)
            .where(cls.id == project_id, cls.prefix.is_not(None))
            .values(ticket_seq=cls.ticket_seq + 1)
            .returning(cls.prefix, cls.ticket_seq)
            .execution_options(synchronize_session=False)
        )
        row = (await db_session().execute(statement)).one_or_none()
        if row:
            prefix, number = row
            return f"{prefix}-{number}"
        project = await cls.get(project_id)
        if not project:
            raise ProjectNotFound(project_id)
        raise MissingPrefix(project.name)

    async def set_prefix(self, prefix: str | None) -> None:
        """Rename the namespace — refused once a ticket has been minted against
        it, because the identifiers already handed out spell the old prefix and
        are never rewritten. The counter is read from the row rather than the
        instance: ``mint_identifier`` bumps it with an UPDATE this session's
        copy has not seen."""
        session = db_session()
        minted = await session.scalar(select(Project.ticket_seq).where(Project.id == self.id))
        # Normalize explicitly for this comparison: @validates only normalizes
        # on assignment, which happens below, after the lock check.
        next_prefix = (
            None if prefix is None or not str(prefix).strip() else normalize_prefix(prefix)
        )
        if minted and next_prefix != self.prefix:
            raise PrefixLocked(self.prefix or "")
        self.prefix = prefix
        self.updated_at = Base.utc_now()
        try:
            await session.flush()
        except IntegrityError as error:
            _raise_prefix_taken(error, next_prefix)
            raise


class ProjectRepo(StoredSubject):
    __tablename__ = "project_repos"
    __table_args__ = (Index("project_repos_project_idx", "project_id"),)

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
    )
    full_name: Mapped[str] = mapped_column(unique=True)
    # Optional free-form role for the dashboard: "design", "infra",
    # "app". None when the operator hasn't labelled it.
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
    async def list_for_tickets(cls) -> list["ProjectRepo"]:
        """Repos whose project can mint an identifier. A project without a
        prefix is not a ticket target yet."""
        statement = (
            select(cls)
            .join(Project)
            .where(Project.prefix.is_not(None))
            .order_by(Project.name, cls.full_name)
        )
        return list(await db_session().scalars(statement))

    @classmethod
    async def get_in_project(cls, *, project_id: int, repo_id: int) -> "ProjectRepo | None":
        # Scoped lookup for the nested /projects/{project_id}/repos/{repo_id} routes:
        # a repo reached through the wrong project's URL is a miss, not a hit to reject.
        stmt = select(cls).where(cls.id == repo_id, cls.project_id == project_id).limit(1)
        return (await db_session().scalars(stmt)).first()

    def get_label(self) -> str:
        return self.full_name

    def get_summary(self) -> "ProjectRepoSummary":
        return ProjectRepoSummary.model_validate(self)

    @classmethod
    async def list_summaries(cls, account_id: str | None) -> list["ProjectRepoSummary"]:
        # A repo is registered, not transient, so the board is all of them by name.
        stmt = select(cls).order_by(cls.full_name)
        return [repo.get_summary() for repo in await db_session().scalars(stmt)]

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
        # {} until the repo profiler has run — an unprofiled repo is a normal state.
        return self.profile.get("effective") or {}

    async def set_profile(self, *, baseline: dict[str, Any], effective: dict[str, Any]) -> None:
        self.profile = {"baseline": baseline, "effective": effective}
        await db_session().flush()

    @classmethod
    async def get_for_name(cls, name: str) -> "ProjectRepo | None":
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
        return (await db_session().scalars(stmt)).first()

    @classmethod
    async def get_for_repo(
        cls, full_name: str, *, raise_on_missing: bool = False
    ) -> "ProjectRepo | None":
        stmt = select(cls).where(func.lower(cls.full_name) == full_name.lower()).limit(1)
        repo = (await db_session().scalars(stmt)).first()
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
    async def lookup(
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
                row = await cls.get_for_name(name)
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
    # Which tracker the ticket lives in: ``linear`` / ``github`` /
    # ``jira`` / ``issues``. Combined with ``ticket_key`` to uniquely identify
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
    async def list_summaries(cls, account_id: str | None) -> list[WorkItemSummary]:
        # Where a run stands colours the row; it never decides whether the row is
        # here. The 500 most-recent cover it; paginate if a board outgrows it.
        stmt = (
            select(cls).where(cls.resolution.is_(None)).order_by(cls.updated_at.desc()).limit(500)
        )
        return [item.get_summary() for item in await db_session().scalars(stmt)]

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
        """The item a pull request belongs to. Its number identifies it; the head
        branch is the fallback for an event that lands before druks mirrored one."""
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
        # cycle: the app imports this module at file scope.
        import druks.contrib.software_factory.app as software_factory_app

        self.resolution = "merged" if merged else "closed"
        self.resolved_at = at
        self.updated_at = Base.utc_now()
        await software_factory_app.SoftwareFactory.record_event(type=self.resolution, subject=self)
        await db_session().flush()

    async def ship(self) -> None:
        # A build parked on the operator's review is stranded by their merge; a running
        # one converges on its own, its merge step finding the PR already closed.
        from druks.contrib.software_factory.workflows import Build

        build = await self.get_status(workflow=Build)
        if build.is_parked:
            await Build.cancel(self, failure="pr merged while parked")
        await self.set_ticket_status(TicketStatus.DONE)

    async def close_external(self) -> None:
        # The attempt was abandoned, not the ticket, so the ticket returns to the
        # provider's resting pool. Branch cleanup is best-effort: a fetch failure
        # must not strand it there.
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
        """The item a ticket names in its tracker — (source, ticket_key) is the
        row's identity."""
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
        # Lazy: the Software Factory app imports this module, so it can't be imported at top.
        import druks.contrib.software_factory.app as software_factory_app

        tracker = await software_factory_app.SoftwareFactory.get_tracker(self.source)
        # No tracker means nothing to sync: a github item, a source the operator
        # has switched away from, or credentials not set yet.
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


# Tickets import Project; register their tables after Project exists.
from druks.contrib.software_factory.issues import models as _issues_models  # noqa: E402, F401
