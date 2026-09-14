import pytest
from druks.database import db_session
from druks.exceptions import SessionNotBoundError
from druks.testing import _ProductionRequest


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
