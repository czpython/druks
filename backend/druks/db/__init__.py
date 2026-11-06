import asyncio
import functools
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_scoped_session, async_sessionmaker

from druks.exceptions import SessionNotBoundError
from druks.models import Model, StoredSubject

__all__ = ["Model", "StoredSubject", "atomic", "db_session"]


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


def _autocommit(operation: Callable) -> Callable:
    @functools.wraps(operation)
    async def run(self: AsyncSession, *args: Any, **kwargs: Any) -> Any:
        if self.info["atomic"]:
            return await operation(self, *args, **kwargs)
        try:
            result = await operation(self, *args, **kwargs)
        except BaseException:
            await self.rollback()
            raise
        await self.commit()
        return result

    return run


class AutocommitSession(AsyncSession):
    """Each operation outside ``atomic()`` commits on its own, so the session holds a
    pooled connection only while a statement runs. Its rows stay attached between
    operations, so ``save()`` writes through it."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Outside atomic(). A session of any other kind is one transaction throughout.
        self.info["atomic"] = False

    execute = _autocommit(AsyncSession.execute)
    scalar = _autocommit(AsyncSession.scalar)
    scalars = _autocommit(AsyncSession.scalars)
    get = _autocommit(AsyncSession.get)
    flush = _autocommit(AsyncSession.flush)
    refresh = _autocommit(AsyncSession.refresh)


@asynccontextmanager
async def atomic() -> AsyncIterator[None]:
    """The writes in the block commit together at its end, or none does. A request is
    one transaction already, so there the block adds nothing."""
    session = db_session()
    if session.info.get("atomic", True):
        yield
        return
    session.info["atomic"] = True
    try:
        yield
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    finally:
        session.info["atomic"] = False
