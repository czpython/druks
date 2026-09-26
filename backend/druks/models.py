import enum
import re
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, Self

from sqlalchemy import DateTime, Enum, Integer, cast, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncAttrs, AsyncSession, async_object_session
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from druks.core.utils.time import ensure_utc
from druks.exceptions import DetachedRowError, SubjectNotFound

if TYPE_CHECKING:
    from druks.durable.schemas import SubjectStatus, SubjectSummary
    from druks.workflows import Workflow

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def snake_name(name: str) -> str:
    # "ProjectRepo" → "project_repo": the durable identity a class spells itself.
    return _CAMEL_BOUNDARY.sub("_", name).lower()


class _UtcDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        return ensure_utc(value) if value else value


class Base(AsyncAttrs, DeclarativeBase):
    # A model declares the Python type and no column type: datetimes are tz-aware
    # UTC, a StrEnum is text under a CHECK named after the enum, list and dict are JSONB.
    type_annotation_map = {
        datetime: _UtcDateTime(),
        enum.StrEnum: Enum(
            enum.StrEnum,
            native_enum=False,
            create_constraint=True,
            length=64,
            values_callable=lambda members: [member.value for member in members],
        ),
        list: JSONB,
        dict: JSONB,
    }

    def __init_subclass__(cls, **kwargs: Any) -> None:
        # A table is named for its app and its class unless the class names it.
        if "__tablename__" not in cls.__dict__ and not cls.__dict__.get("__abstract__"):
            # Cycle: the loader is built on this module's Base.
            from druks.apps.loader import resolve_workflow_app

            app = resolve_workflow_app(cls.__module__)
            cls.__tablename__ = f"{app}_{snake_name(cls.__name__)}"
        super().__init_subclass__(**kwargs)

    @property
    def session(self) -> AsyncSession:
        """The session this row is loaded in; a mutation writes through it."""
        if session := async_object_session(self):
            return session
        raise DetachedRowError(type(self).__name__)

    @staticmethod
    def utc_now() -> datetime:
        return datetime.now(UTC).replace(microsecond=0)


class StoredSubject(Base):
    """A row an app's runs are about — a work item, a repo, a document.
    Subclass it instead of ``Base``: the class name is the subject type, so
    ``WorkItem`` is ``work_item``."""

    __abstract__ = True

    subject_type: ClassVar[str]
    # The header its board and page show it under. Set a ``SubjectSummary``
    # subclass to add the app's own fields and a descriptive ``title``.
    summary_class: ClassVar["type[SubjectSummary]"]

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=Base.utc_now, onupdate=Base.utc_now)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        cls.subject_type = snake_name(cls.__name__)
        if "summary_class" not in cls.__dict__:
            # Cycle: the durable read side is built on this module's Base.
            from druks.durable.schemas import SubjectSummary

            cls.summary_class = SubjectSummary
        super().__init_subclass__(**kwargs)

    @property
    def identity(self) -> dict[str, Any]:
        """What a run or an event records in place of the row, which can be gone by
        the time either is read."""
        if self.id:
            return {"type": self.subject_type, "id": self.id}
        raise ValueError(f"unsaved {type(self).__name__} has no identity — flush it first")

    def get_key(self) -> str:
        """The stable work key, such as a ticket key or PR number. Events record
        the descriptive title from get_summary() beside this key."""
        return f"{self.subject_type.replace('_', ' ')} {self.id}"

    @property
    def key(self) -> str:
        return self.get_key()

    @classmethod
    async def create(cls, **fields: object) -> Self:
        """A saved row, flushed so it carries its id."""
        # Cycle: the session seam is built on this module's Base.
        from druks.db import db_session

        row = cls(**fields)
        db_session().add(row)
        await db_session().flush()
        return row

    async def save(self) -> None:
        await self.session.flush()

    async def delete(self) -> None:
        await self.session.delete(self)
        await self.session.flush()

    async def announce(self, topic: str, **facts: Any) -> None:
        """Record and deliver a domain fact in the current transaction."""
        # The event log is built on this module's Base.
        from druks.db import db_session
        from druks.events.models import Event

        await Event.announce(db_session(), self, topic, facts)

    @classmethod
    async def get_for_id(
        cls, subject_id: int | str, *, raise_on_missing: bool = False
    ) -> Self | None:
        """The row this subject id names. A subject id is free text and reaches the
        read-side straight off a URL, so an id this table could never hold is a miss
        rather than an error. ``raise_on_missing`` makes a miss ``SubjectNotFound``:
        the API answers it with 404 and a page with an empty state."""
        from druks.db import db_session

        row = None
        with suppress(ValueError):
            row = await db_session().get(cls, int(subject_id))
        if row or not raise_on_missing:
            return row
        raise SubjectNotFound(cls.subject_type, subject_id)

    def get_summary(self) -> "SubjectSummary":
        return self.summary_class.model_validate(self)

    @classmethod
    async def list_summaries(cls, account_id: str | None) -> "Sequence[SubjectSummary]":
        """The rows on this class's board, newest movement first, each as its domain
        summary. ``account_id`` is the caller, or None outside a request; this shared
        board ignores it. Override to scope the board by caller or to select
        differently."""
        from druks.db import db_session

        # The newest hundred cover a board; a bigger one selects for itself.
        statement = select(cls).order_by(cls.updated_at.desc(), cls.id.desc()).limit(100)
        return [row.get_summary() for row in await db_session().scalars(statement)]

    async def get_status(self, *, workflow: "type[Workflow] | None" = None) -> "SubjectStatus":
        from druks.db import db_session
        from druks.durable.reads import get_subject_status

        return await get_subject_status(
            db_session(), self.subject_type, str(self.id), workflow=workflow
        )

    @classmethod
    async def get_statuses(cls, subject_ids: Sequence[str | int]) -> "dict[str, SubjectStatus]":
        """Where a whole board stands, keyed by subject id — one read for the whole
        board, so a page listing rows does not ask once per row."""
        from druks.db import db_session
        from druks.durable.reads import get_subject_statuses

        return await get_subject_statuses(
            db_session(), cls.subject_type, [str(subject_id) for subject_id in subject_ids]
        )

    async def get_phase(self) -> str | None:
        from druks.db import db_session
        from druks.durable.reads import get_subject_phase

        return await get_subject_phase(db_session(), self.subject_type, str(self.id))

    @classmethod
    async def list_open(cls, *, limit: int = 50) -> list[Self]:
        """The rows whose newest run hasn't handed off — still going, or failed
        and wanting the operator. What an app's active view lists."""
        # Cycle: the durable read side is built on this module's Base.
        from druks.db import db_session
        from druks.durable.models import Run

        # The durable layer keys subjects by string, so the open ids come back as
        # text and cast to this table's integer key.
        open_ids = Run.open_subject_ids(cls.subject_type).subquery()
        stmt = (
            select(cls)
            .where(cls.id.in_(select(cast(open_ids.c.subject_id, Integer))))
            .order_by(cls.id.desc())
            .limit(limit)
        )
        return list(await db_session().scalars(stmt))
