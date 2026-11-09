from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession

from druks.database import session_scope
from druks.settings import Settings, get_settings


def get_engine(request: Request) -> Engine:
    return request.app.state.engine


async def request_session() -> AsyncIterator[AsyncSession]:
    """One transaction per request. The app declares it for every route, and a
    route that names ``SessionDep`` receives that same session."""
    async with session_scope() as session:
        yield session


SettingsDep = Annotated[Settings, Depends(get_settings)]
EngineDep = Annotated[Engine, Depends(get_engine)]
SessionDep = Annotated[AsyncSession, Depends(request_session)]


async def commit_before_response(session: SessionDep) -> AsyncIterator[None]:
    """Commit the request's transaction before the response goes out, so a
    failed commit answers 500, not a success for writes that never landed.
    FastAPI closes ``request_session`` only after it sends the response; the
    app declares this with function scope, which closes first."""
    yield
    await session.commit()
