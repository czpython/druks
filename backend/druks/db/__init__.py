import asyncio

from sqlalchemy.ext.asyncio import AsyncSession, async_scoped_session, async_sessionmaker

from druks.exceptions import SessionNotBoundError
from druks.models import Base, StoredSubject

__all__ = ["Base", "StoredSubject", "db_session"]


def _task_scope() -> object | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return


def _unbound_session() -> AsyncSession:
    raise SessionNotBoundError


# The session author code reads through: one per task, bound by the request
# dependency, a step, and session_scope. Internal code takes a session
# parameter instead.
db_session: async_scoped_session = async_scoped_session(
    async_sessionmaker(class_=AsyncSession, autoflush=True, expire_on_commit=False),
    scopefunc=_task_scope,
)
# A read outside those seams fails instead of opening a session nothing closes.
db_session.registry.createfunc = _unbound_session
