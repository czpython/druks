from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException, Request, WebSocket
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import HTTPConnection

from druks.accounts.context import current_account_id
from druks.accounts.enums import OperatorWrites
from druks.accounts.exceptions import (
    AuthConfigurationError,
    InvalidAssertionError,
    InvalidPatError,
)
from druks.accounts.jwt import verify_assertion
from druks.accounts.models import Account, OperatorToken, PersonalAccessToken
from druks.api.dependencies import SessionDep

_BEARER_CHALLENGE = 'Bearer realm="druks"'
# auto_error=False: absence and malformed both come back None — presence is
# checked separately so a malformed header hard-fails instead of sliding to
# the session identity. Registers the bearer scheme in the OpenAPI schema.
_bearer_scheme = HTTPBearer(auto_error=False, scheme_name="personalAccessToken")


async def resolve_pat_account(
    session: AsyncSession, request: HTTPConnection, credentials: HTTPAuthorizationCredentials | None
) -> Account:
    """A present Authorization must authenticate — never a fall-through. A
    token limited to agent tools passes only their routes, and an operator
    token that may not write passes only reads."""
    if credentials:
        try:
            operator = await OperatorToken.lookup(credentials.credentials)
            if operator:
                # The box holds this token and this appliance's URL, so its
                # write mode has to hold here, not only on the MCP tools.
                if request.scope["method"] == "GET" or operator.writes == OperatorWrites.ALLOW:
                    return await Account.get_for_run(session, operator.account_id)
                raise HTTPException(status_code=403, detail="This operator token may only read.")
            pat = await PersonalAccessToken.authenticate(session, credentials.credentials)
        except InvalidPatError as error:
            raise HTTPException(
                status_code=401,
                detail=str(error),
                headers={"WWW-Authenticate": f'{_BEARER_CHALLENGE}, error="invalid_token"'},
            ) from error
        if pat.allowed_tools is None:
            return pat.account
        # The MCP surface maps each agent route's endpoint to its tool name at boot.
        if request.app.state.agent_tools.get(request.scope["endpoint"]) in pat.allowed_tools:
            return pat.account
        raise HTTPException(
            status_code=403,
            detail=(
                f"Token {pat.token_prefix} is limited to these tools: "
                f"{', '.join(pat.allowed_tools)}."
            ),
        )
    raise HTTPException(
        status_code=401,
        detail="Authorization must be: Bearer <token>.",
        headers={"WWW-Authenticate": _BEARER_CHALLENGE},
    )


async def resolve_single_operator(session: AsyncSession) -> Account | None:
    """None while zero accounts exist (setup); more than one refuses rather
    than guesses."""
    operators = await Account.list_all(session)
    if len(operators) > 1:
        raise AuthConfigurationError(
            f"auth mode 'none' expects exactly one operator account, found "
            f"{len(operators)} — remove the extras or switch to header mode"
        )
    return operators[0] if operators else None


async def _resolve_operator(session: AsyncSession, connection: HTTPConnection) -> Account | None:
    """None only during none/zero setup. header maps the asserted email; jwt
    maps its verified identity claim; none ignores the header entirely. Takes a
    connection, not a request, so a WebSocket upgrade resolves the same way."""
    settings = connection.app.state.settings
    if settings.identity.mode == "none":
        return await resolve_single_operator(session)
    values = connection.headers.getlist(settings.identity.header)
    if len(values) == 1 and (asserted := values[0].strip()):
        if settings.identity.mode == "header":
            return await Account.get_or_create(session, asserted)
        try:
            email = await verify_assertion(asserted, settings)
        except InvalidAssertionError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        return await Account.get_or_create(session, email)
    raise HTTPException(
        status_code=401,
        detail=f"The edge must assert exactly one nonblank {settings.identity.header} identity.",
    )


def _require_no_bearer(connection: HTTPConnection) -> None:
    if "Authorization" in connection.headers:
        raise HTTPException(
            status_code=401,
            detail="This API accepts your edge or local operator identity only, "
            "never a bearer token.",
        )


async def require_operator(session: AsyncSession, websocket: WebSocket) -> Account:
    """The operator behind a WebSocket upgrade, resolved from the same edge
    identity HTTP uses — druks re-asserts it here rather than trusting the edge,
    as every /api route does."""
    _require_no_bearer(websocket)
    account = await _resolve_operator(session, websocket)
    if not account:
        raise HTTPException(
            status_code=409,
            detail="No operator account exists yet — connect a provider to finish setup.",
        )
    return account


async def current_account(
    request: Request,
    session: SessionDep,
    bearer: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> AsyncIterator[Account]:
    """The Bearer PAT when Authorization is present — present-but-empty still
    challenges — else the session identity."""
    if "Authorization" in request.headers:
        account = await resolve_pat_account(session, request, bearer)
    else:
        account = await _resolve_operator(session, request)
        if not account:
            raise HTTPException(
                status_code=409,
                detail="No operator account exists yet — connect a provider to finish setup.",
            )
    token = current_account_id.set(account.id)
    try:
        yield account
    finally:
        # The actor must not leak into whatever runs on this task next.
        current_account_id.reset(token)


async def current_session_account(request: Request, session: SessionDep) -> AsyncIterator[Account]:
    """The signed-in human, never a bearer — a token cannot manage
    capabilities. Identity re-asserts per request; no session state."""
    _require_no_bearer(request)
    account = await _resolve_operator(session, request)
    if not account:
        raise HTTPException(
            status_code=409,
            detail="No operator account exists yet — connect a provider to finish setup.",
        )
    token = current_account_id.set(account.id)
    try:
        yield account
    finally:
        current_account_id.reset(token)


async def current_session_or_setup(
    request: Request, session: SessionDep
) -> AsyncIterator[Account | None]:
    """The signed-in human; None during none/zero setup, where the first
    completed connection creates the operator."""
    _require_no_bearer(request)
    account = await _resolve_operator(session, request)
    token = current_account_id.set(account.id if account else None)
    try:
        yield account
    finally:
        current_account_id.reset(token)


async def current_account_or_setup(
    request: Request,
    session: SessionDep,
    bearer: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> AsyncIterator[Account | None]:
    """PAT-first identity that reads none/zero setup as None instead of
    refusing — ``/api/auth/me`` only."""
    if "Authorization" in request.headers:
        account = await resolve_pat_account(session, request, bearer)
    else:
        account = await _resolve_operator(session, request)
    token = current_account_id.set(account.id if account else None)
    try:
        yield account
    finally:
        current_account_id.reset(token)
