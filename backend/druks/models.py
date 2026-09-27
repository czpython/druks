import enum
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Self

from pydantic import TypeAdapter
from sqlalchemy import (
    DateTime,
    Enum,
    Integer,
    Select,
    UnaryExpression,
    cast,
    desc,
    false,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncAttrs, AsyncSession, async_object_session
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from druks.core.utils.time import ensure_utc
from druks.exceptions import DetachedRowError, ObjectNotFound

if TYPE_CHECKING:
    from druks.durable.schemas import SubjectStatus, SubjectSummary
    from druks.workflows import Workflow

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def snake_name(name: str) -> str:
    # "ProjectRepo" → "project_repo": the durable identity a class spells itself.
    return _CAMEL_BOUNDARY.sub("_", name).lower()


# A StrEnum or Literal column stores its value as text under a CHECK of the allowed
# values, so the database refuses what the Python type refuses.
_CHOICES = Enum(
    enum.StrEnum,
    native_enum=False,
    create_constraint=True,
    length=64,
    values_callable=lambda members: [member.value for member in members],
)


class _UtcDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        return ensure_utc(value) if value else value


class Base(AsyncAttrs, DeclarativeBase):
    # A model declares the Python type and no column type: datetimes are tz-aware
    # UTC, a StrEnum or Literal is checked text, and list and dict are JSONB.
    type_annotation_map = {
        datetime: _UtcDateTime(),
        enum.StrEnum: _CHOICES,
        Literal: _CHOICES,
        list: JSONB,
        dict: JSONB,
    }

    @property
    def session(self) -> AsyncSession:
        """The session this row is loaded in; a mutation writes through it."""
        if session := async_object_session(self):
            return session
        raise DetachedRowError(type(self).__name__)

    @staticmethod
    def utc_now() -> datetime:
        return datetime.now(UTC).replace(microsecond=0)


class Model(Base):
    """A table an app keeps. Druks names the table for the app and the class, and a
    row reads and writes through the session of the request or step it runs in."""

    __abstract__ = True

    # The order ``filter`` and a subject's board read rows in, as the class declared
    # it: ``class Report(Model, ordering=("-created_at",))`` is newest first.
    _ordering: ClassVar[tuple[str, ...]] = ()

    def __init_subclass__(cls, ordering: tuple[str, ...] = (), **kwargs: Any) -> None:
        if ordering:
            cls._ordering = ordering
        # A table is named for its app and its class unless the class names it.
        if "__tablename__" not in cls.__dict__ and not cls.__dict__.get("__abstract__"):
            # Cycle: the loader is built on this module's Base.
            from druks.apps.loader import resolve_workflow_app

            app = resolve_workflow_app(cls.__module__)
            cls.__tablename__ = f"{app}_{snake_name(cls.__name__)}"
        super().__init_subclass__(**kwargs)

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

    @classmethod
    async def get(cls, **fields: object) -> Self:
        """The one row whose fields hold these values. A miss raises ``ObjectNotFound``,
        which a route answers with 404 and a page with an empty state."""
        if row := await cls.get_or_none(**fields):
            return row
        raise ObjectNotFound(snake_name(cls.__name__).replace("_", " "), fields)

    @classmethod
    async def get_or_none(cls, **fields: object) -> Self | None:
        """The one row whose fields hold these values, or None. Two rows raise
        SQLAlchemy's ``MultipleResultsFound``."""
        from druks.db import db_session

        return (await db_session().scalars(cls._select_matching(fields))).one_or_none()

    @classmethod
    async def filter(cls, **fields: object) -> list[Self]:
        """The rows whose fields hold these values, in the class's ordering, or in
        primary key order when it declares none."""
        from druks.db import db_session

        ordering = cls._get_order_by() or cls.__mapper__.primary_key
        statement = cls._select_matching(fields).order_by(*ordering)
        return list(await db_session().scalars(statement))

    @classmethod
    def _get_order_by(cls) -> list[str | UnaryExpression[object]]:
        # Django's form: a leading "-" sorts that column descending.
        return [
            desc(name.removeprefix("-")) if name.startswith("-") else name for name in cls._ordering
        ]

    @classmethod
    def _select_matching(cls, fields: dict[str, object]) -> Select[tuple[Self]]:
        # Ids arrive as text off a URL or an event, so a text value is read as its
        # column's type, and one the column could never hold matches no row.
        values = dict(fields)
        for name, value in fields.items():
            if type(value) is str and name in cls.__table__.columns:
                column_type = cls.__table__.columns[name].type.python_type
                try:
                    values[name] = TypeAdapter(column_type).validate_python(value)
                except ValueError:
                    return select(cls).where(false())
        return select(cls).filter_by(**values)


class StoredSubject(Model):
    """A row an app's runs are about — a work item, a repo, a document.
    Subclass it instead of ``Model``: the class name is the subject type, so
    ``WorkItem`` is ``work_item``."""

    __abstract__ = True

    subject_type: ClassVar[str]

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=Base.utc_now, onupdate=Base.utc_now)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        cls.subject_type = snake_name(cls.__name__)
        super().__init_subclass__(**kwargs)

    @property
    def identity(self) -> dict[str, Any]:
        """What a run or an event records in place of the row, which can be gone by
        the time either is read."""
        if self.id:
            return {"type": self.subject_type, "id": self.id}
        raise ValueError(f"unsaved {type(self).__name__} has no identity — flush it first")

    def __str__(self) -> str:
        """How the subject shows itself on runs, events, the Activity feed, and its
        board. Its type and id identify it, so the name need not be unique."""
        return f"{self.subject_type.replace('_', ' ')} {self.id}"

    @property
    def key(self) -> str:
        return str(self)

    async def announce(self, topic: str, **facts: Any) -> None:
        """Record and deliver a domain fact in the current transaction."""
        # The event log is built on this module's Base.
        from druks.db import db_session
        from druks.events.models import Event

        await Event.announce(db_session(), self, topic, facts)

    def get_summary(self) -> "SubjectSummary":
        """The header the platform's own screens show. An app with its own frontend
        overrides it to add the fields that frontend reads."""
        # Cycle: the durable read side is built on this module's Base.
        from druks.durable.schemas import SubjectSummary

        return SubjectSummary.model_validate(self)

    @classmethod
    async def list_summaries(cls, account_id: str | None) -> "Sequence[SubjectSummary]":
        """The rows on this class's board, in the class's ordering or newest movement
        first, each as its summary. ``account_id`` is the caller, or None outside a
        request; this shared board ignores it. Override to scope the board by caller
        or to select differently."""
        from druks.db import db_session

        ordering = cls._get_order_by() or [cls.updated_at.desc(), cls.id.desc()]
        # The first hundred cover a board; a bigger one selects for itself.
        statement = select(cls).order_by(*ordering).limit(100)
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
