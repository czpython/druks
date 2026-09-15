from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession

from druks.database import session_scope
from druks.settings import Settings


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


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
