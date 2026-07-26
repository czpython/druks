import re
from datetime import UTC, datetime
from typing import Any, ClassVar, Self

from sqlalchemy import DateTime, Integer, cast, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from druks.core.utils.time import ensure_utc

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def snake_name(name: str) -> str:
    # "ProjectRepo" → "project_repo": the durable identity a class spells itself.
    return _CAMEL_BOUNDARY.sub("_", name).lower()


class _UtcDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        return ensure_utc(value) if value else value


class Base(DeclarativeBase):
    # Every ``Mapped[datetime]`` column stores tz-aware UTC — the decorator
    # guarantees aware values on read (writes are unaffected). Mapping it here
    # means models declare ``Mapped[datetime]`` with no per-column type.
    type_annotation_map = {datetime: _UtcDateTime()}

    @staticmethod
    def utc_now() -> datetime:
        return datetime.now(UTC).replace(microsecond=0)


class StoredSubject(Base):
    """A row an extension's runs are about — a work item, a repo, a document.
    Subclass it instead of ``Base``: the class name is the subject type, so
    ``WorkItem`` is ``work_item``."""

    __abstract__ = True

    subject_type: ClassVar[str]

    id: Mapped[int] = mapped_column(primary_key=True)

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

    @classmethod
    def list_open(cls, *, limit: int = 50) -> list[Self]:
        """The rows whose newest run hasn't handed off — still going, or failed
        and wanting the operator. What an extension's active view lists."""
        # Cycle: the durable read side is built on this module's Base.
        from druks.database import db_session
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
        return list(db_session().scalars(stmt))
