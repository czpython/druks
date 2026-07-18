import logging
import re
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, func, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from druks.build.enums import HandoffStatus
from druks.db import Base, db_session
from druks.durable.reads import get_subject_status
from druks.durable.schemas import SubjectStatus
from druks.ticketing.datastructures import Ticket
from druks.ticketing.enums import SemanticStatus
from druks.ticketing.exceptions import TrackerNotConfigured
from druks.ticketing.helpers import get_tracker, is_tracker_source
from druks.workflows import AgentCall, Run

logger = logging.getLogger(__name__)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Human label shown in the dashboard. Unique because we use it as
    # the natural display key — duplicate names would make the project
    # column ambiguous.
    name: Mapped[str] = mapped_column(unique=True)
    slug: Mapped[str] = mapped_column(unique=True)
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    repos: Mapped[list["ProjectRepo"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @classmethod
    def create(cls, *, name: str, slug: str | None = None) -> "Project":
        session = db_session()
        project = cls(name=name, slug=slug or slugify(name))
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


class ProjectRepo(Base):
    __tablename__ = "project_repos"
    __table_args__ = (Index("project_repos_project_idx", "project_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
    )
    # ``owner/name``. UNIQUE because a repo lives in exactly one project
    # If multi-project sharing ever becomes a
    # real requirement, drop the UNIQUE and route the lookup through a
    # primary-binding column instead.
    full_name: Mapped[str] = mapped_column(unique=True)
    # Optional free-form role for the dashboard: "design", "infra",
    # "extension". None when the operator hasn't labelled it.
    purpose: Mapped[str | None]
    # {"baseline": ..., "effective": ...} — baseline is what the repo profiler
    # detected; effective is baseline with the operator's pinned verification
    # (RepoPolicy.verification) applied. Kept separate so removing the pin
    # restores the detected baseline instead of losing it.
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

    def effective_profile(self) -> dict[str, Any]:
        # {} until the repo profiler has run — an unprofiled repo is a normal state.
        return self.profile.get("effective") or {}

    def profiler_status(self) -> SubjectStatus:
        return get_subject_status("project_repo", str(self.id))

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
            return None
        # SQLite-friendly bare-name suffix match.
        stmt = select(cls).where(func.lower(cls.full_name).like(f"%/{target}")).limit(1)
        return db_session().scalars(stmt).first()

    @classmethod
    def get_for_repo(cls, full_name: str) -> "ProjectRepo | None":
        stmt = select(cls).where(func.lower(cls.full_name) == full_name.lower()).limit(1)
        return db_session().scalars(stmt).first()

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
        return None


def slugify(name: str) -> str:
    """Lowercase, replace non-alnum runs with single hyphens, trim ends."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


class WorkItem(Base):
    __tablename__ = "work_items"
    __table_args__ = (
        Index("work_items_repo_idx", "repo", "pr_number"),
        # One WorkItem per (source, remote_key) - i.e. one row per ticket
        # in the remote tracker. Partial because GitHub-source rows
        # without an ``#NNN`` ticket reference can still exist; the
        # constraint only fires when remote_key is set. ``source`` is
        # part of the key so Linear "ABC-1" and Jira "ABC-1" don't
        # collide once we support multiple providers.
        Index(
            "work_items_remote_unique",
            "source",
            "remote_key",
            unique=True,
            sqlite_where=text("remote_key IS NOT NULL"),
        ),
        Index("work_items_project_idx", "project_id"),
        Index("work_items_status_idx", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # The Druks Project this WorkItem belongs to. Required - intake
    # refuses tickets whose Linear project doesn't map to a ProjectRepo.
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id"),
    )
    project: Mapped[Project] = relationship(lazy="joined")
    # Which remote tracker the ticket lives in: ``linear`` / ``github`` /
    # future ``jira``. Combined with ``remote_key`` to uniquely identify
    # a ticket.
    source: Mapped[str] = mapped_column(default="github")
    title: Mapped[str] = mapped_column(default="")
    # Human-readable issue key in the source: ``ACME-270`` / ``#42`` /
    # ``JIRA-123``. Linear's GraphQL accepts the identifier wherever it
    # accepts the UUID.
    remote_key: Mapped[str | None]
    remote_url: Mapped[str | None]
    # The PR-target repo. Still on WorkItem (not derived from project)
    # because a Project can hold N repos but every WorkItem PRs into one.
    repo: Mapped[str]
    pr_number: Mapped[int | None]
    branch: Mapped[str | None]
    # The item's durable build run. Build owns the work-item ↔ run link here, so
    # the platform run table stays oblivious to extensions; it's the dedup anchor (one
    # active build per item) and the resume handle. SET NULL: a pruned run leaves
    # the item with no active build, which is what the dedup check already reads.
    build_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("durable_runs.id", ondelete="SET NULL"), default=None
    )
    # The handoff lane: null while in flight, a HandoffStatus at rest. active =
    # null, history = set; stamped at handoff, cleared on (re)dispatch.
    status: Mapped[str | None] = mapped_column(default=None)
    # Intake-time snapshot of the resolved ``.druks/build`` config; the
    # workflow reads it for the item's lifespan so a mid-flight config
    # push can't flip policy under a running build. Empty only until
    # intake fills it in (the workflow then resolves live).
    extension_config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    @classmethod
    def subject_for(cls, work_item_id: int) -> dict[str, Any]:
        """The event-log subject for a work item — what build stamps on a run at
        dispatch and on the milestones it emits, so both key to the same item.
        Takes the id (not an instance): the merge step and dispatch only hold it."""
        return {"type": "work_item", "id": work_item_id}

    @classmethod
    def create(
        cls,
        *,
        project_id: int,
        source: str = "github",
        title: str,
        remote_key: str | None = None,
        remote_url: str | None = None,
        repo: str,
    ) -> "WorkItem":
        session = db_session()
        item = cls(
            project_id=project_id,
            source=source,
            title=title,
            remote_key=remote_key,
            remote_url=remote_url,
            repo=repo,
        )
        session.add(item)
        session.flush()
        return item

    @classmethod
    def get(cls, work_item_id: int) -> "WorkItem | None":
        return db_session().get(cls, work_item_id)

    @classmethod
    def get_for_pr(cls, *, repo: str, pr_number: int) -> "WorkItem | None":
        stmt = (
            select(cls)
            .where(func.lower(cls.repo) == repo.lower(), cls.pr_number == pr_number)
            .order_by(cls.updated_at.desc())
            .limit(1)
        )
        return db_session().scalars(stmt).first()

    @classmethod
    def get_for_branch(cls, *, repo: str, branch: str) -> "WorkItem | None":
        stmt = (
            select(cls)
            .where(func.lower(cls.repo) == repo.lower(), cls.branch == branch)
            .order_by(cls.updated_at.desc())
            .limit(1)
        )
        return db_session().scalars(stmt).first()

    @classmethod
    def is_known_druks_pr(
        cls,
        *,
        repo: str,
        pr_number: int | None,
        branch: str | None,
        include_terminal: bool = False,
    ) -> bool:
        """Does an item druks owns match this PR/branch? ``include_terminal``
        also accepts items whose build run has finished — the close echo still
        needs to find a merged item that no longer has a live run."""
        by_pr = cls.get_for_pr(repo=repo, pr_number=pr_number) if pr_number else None
        by_branch = cls.get_for_branch(repo=repo, branch=branch) if branch else None
        for item in (by_pr, by_branch):
            if item and (include_terminal or item.has_live_build()):
                return True
        return False

    def get_build_run(self) -> "Run | None":
        return Run.get(self.build_run_id) if self.build_run_id else None

    def has_live_build(self) -> bool:
        run = self.get_build_run()
        return bool(run and run.is_active)

    async def cancel_active_build(self, *, failure: str) -> None:
        """Cancel the item's active build run. Called when the PR left druks's
        hands (merged or closed externally)."""
        run = self.get_build_run()
        if run and run.is_active:
            await run.cancel(failure=failure)
        db_session().flush()

    @classmethod
    def get_for_remote_key(
        cls,
        *,
        source: str,
        remote_key: str,
    ) -> "WorkItem | None":
        """Look up a WorkItem by its source + remote_key pair.

        The (source, remote_key) unique constraint guarantees at most
        one live row; we return that row or None if no match exists."""
        stmt = select(cls).where(cls.source == source, cls.remote_key == remote_key).limit(1)
        return db_session().scalars(stmt).first()

    @classmethod
    def by_remote_key(
        cls,
        *,
        source: str,
        remote_keys: set[str],
    ) -> dict[str, "WorkItem"]:
        """Bulk variant of ``get_for_remote_key`` keyed by remote_key.

        Returns a dict mapping each found key to its WorkItem; missing
        keys are simply absent from the returned dict."""
        if not remote_keys:
            return {}
        stmt = select(cls).where(
            cls.source == source,
            cls.remote_key.in_(remote_keys),
        )
        return {wi.remote_key: wi for wi in db_session().scalars(stmt) if wi.remote_key}

    @classmethod
    def list_recent(cls, *, limit: int = 50, offset: int = 0) -> list["WorkItem"]:
        stmt = select(cls).order_by(cls.updated_at.desc()).limit(limit).offset(offset)
        return list(db_session().scalars(stmt))

    @classmethod
    def list_handoff(cls, *, limit: int = 10) -> list["WorkItem"]:
        # The history list: items at rest in a handoff lane, newest first.
        stmt = (
            select(cls).where(cls.status.is_not(None)).order_by(cls.updated_at.desc()).limit(limit)
        )
        return list(db_session().scalars(stmt))

    @classmethod
    def sandbox_host_id_for(cls, repo: str, pr_number: int) -> str | None:
        """The host_id of the most recent agent run for this work item. None
        when nothing has run for this PR yet.

        Callers MUST tolerate stale ids — the host may already be gone on the
        provider side. ``sandbox_client.attach`` translates a 404 into
        :class:`HostGone`, and the for_pr / token-rotation paths catch that and
        re-acquire or skip.
        """
        item = cls.get_for_pr(repo=repo, pr_number=pr_number)
        if not item:
            return None
        calls = AgentCall.list_for_subject("work_item", str(item.id))
        return calls[-1].sandbox_host_id if calls else None

    def set_status(
        self, status: HandoffStatus | None, *, event_payload: dict[str, Any] | None = None
    ) -> None:
        """The handoff-lane write. A non-None status is a milestone, so the
        matching build event records first — the pairing is structural, not a
        call-site convention. None clears the lane on (re)dispatch: no event."""
        if status:
            # cycle: the extension imports this module at file scope.
            from druks.build.extension import Build

            Build.record_event(
                type=status, subject=self.subject_for(self.id), payload=event_payload
            )
        self.status = status
        self.updated_at = Base.utc_now()
        db_session().flush()

    async def set_remote_status(self, status: SemanticStatus) -> None:
        # No-op for sources without a configured tracker (github, absent creds).
        if not is_tracker_source(self.source):
            return
        # Lazy: the Build extension imports this module, so it can't be imported at top.
        from druks.build.extension import Build

        try:
            tracker = get_tracker(
                self.source, ready_for_agent_status=Build.post_refinement_status(self.source)
            )
        except TrackerNotConfigured:
            return

        ticket = Ticket.ref(self.source, self.remote_key)
        async with tracker:
            try:
                await tracker.set_status(ticket, status)
            except (ValueError, *tracker.known_exceptions):
                logger.warning(
                    "Could not sync %s ticket %s to %s.",
                    self.source,
                    self.remote_key,
                    status.value,
                    exc_info=True,
                )

    def update(
        self,
        *,
        title: str | None = None,
        remote_url: str | None = None,
        pr_number: int | None = None,
        branch: str | None = None,
        build_run_id: str | None = None,
        project_id: int | None = None,
        extension_config_snapshot: dict[str, Any] | None = None,
    ) -> None:
        if title is not None:
            self.title = title
        if extension_config_snapshot is not None:
            self.extension_config_snapshot = extension_config_snapshot
        if remote_url is not None:
            self.remote_url = remote_url
        if pr_number is not None:
            self.pr_number = pr_number
        if branch is not None:
            self.branch = branch
        if build_run_id is not None:
            self.build_run_id = build_run_id
        if project_id is not None:
            self.project_id = project_id
        self.updated_at = Base.utc_now()
        db_session().flush()
