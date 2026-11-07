import httpx
import pytest
from druks.api.dependencies import SessionDep, commit_before_response, request_session
from druks.database import configure_session
from druks.db import db_session
from druks.exceptions import SessionNotBoundError
from druks.testing import TEST_DATABASE_URL, _ProductionRequest, asgi_client
from fastapi import Depends, FastAPI
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import create_async_engine


async def test_a_request_holds_no_session_and_opens_none(druks_db):
    # The test client runs a request on this task, which holds a harness
    # session. Production holds none, so the boundary hides it for the request.
    before = db_session()
    bound = []

    async def app(scope, receive, send):
        bound.append(db_session.registry.has())
        with pytest.raises(SessionNotBoundError):
            db_session()

    await _ProductionRequest(app)({"type": "http"}, None, None)
    assert bound == [False]
    assert db_session() is before


async def test_a_route_receives_the_request_session_the_app_bound(druks_db):
    app = FastAPI(dependencies=[Depends(request_session)])

    @app.get("/")
    async def probe(session: SessionDep) -> dict[str, bool]:
        return {"same": session is db_session()}

    async with asgi_client(app) as client:
        assert (await client.get("/")).json() == {"same": True}


async def test_a_request_whose_commit_fails_answers_500():
    # A deferred check fails only at COMMIT. Its own engine, not druks_db:
    # there a commit only releases a savepoint.
    configure_session(create_async_engine(TEST_DATABASE_URL, poolclass=NullPool))
    app = FastAPI(dependencies=[Depends(commit_before_response, scope="function")])

    @app.post("/")
    async def write(session: SessionDep) -> None:
        await session.execute(
            text("CREATE TEMP TABLE claims (id int UNIQUE DEFERRABLE INITIALLY DEFERRED)")
        )
        await session.execute(text("INSERT INTO claims VALUES (1), (1)"))

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        assert (await client.post("/")).status_code == 500
