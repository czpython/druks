from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from druks.accounts.context import current_account_id
from druks.accounts.exceptions import (
    AuthConfigurationError,
    InvalidAssertionError,
    InvalidPatError,
)
from druks.accounts.jwt import verify_assertion
from druks.accounts.models import Account, PersonalAccessToken

_BEARER_CHALLENGE = 'Bearer realm="druks"'
# auto_error=False: absence and malformed both come back None — presence is
# checked separately so a malformed header hard-fails instead of sliding to
# the session identity. Registers the bearer scheme in the OpenAPI schema.
_bearer_scheme = HTTPBearer(auto_error=False, scheme_name="personalAccessToken")


def resolve_pat_account(credentials: HTTPAuthorizationCredentials | None) -> Account:
    """A present Authorization must authenticate — never a fall-through."""
    if credentials:
        try:
            return PersonalAccessToken.authenticate(credentials.credentials).account
        except InvalidPatError as error:
            raise HTTPException(
                status_code=401,
                detail=str(error),
                headers={"WWW-Authenticate": f'{_BEARER_CHALLENGE}, error="invalid_token"'},
            ) from error
    raise HTTPException(
        status_code=401,
        detail="Authorization must be: Bearer <token>.",
        headers={"WWW-Authenticate": _BEARER_CHALLENGE},
    )


def resolve_single_operator() -> Account | None:
    """None while zero accounts exist (setup); more than one refuses rather
    than guesses."""
    operators = Account.list_non_system()
    if len(operators) > 1:
        raise AuthConfigurationError(
            f"auth mode 'none' expects exactly one operator account, found "
            f"{len(operators)} — remove the extras or switch to header mode"
        )
    return operators[0] if operators else None


async def _resolve_operator(request: Request) -> Account | None:
    """None only during none/zero setup. header maps the asserted email; jwt
    maps its verified identity claim; none ignores the header entirely."""
    settings = request.app.state.settings
    if settings.auth_mode == "none":
        return resolve_single_operator()
    values = request.headers.getlist(settings.auth_header)
    if len(values) == 1 and (asserted := values[0].strip()):
        if settings.auth_mode == "header":
            return Account.get_or_create(asserted)
        try:
            email = await verify_assertion(asserted, settings)
        except InvalidAssertionError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        return Account.get_or_create(email)
    raise HTTPException(
        status_code=401,
        detail=f"The edge must assert exactly one nonblank {settings.auth_header} identity.",
    )


def _require_no_bearer(request: Request) -> None:
    if "Authorization" in request.headers:
        raise HTTPException(
            status_code=401,
            detail="This API accepts your edge or local operator identity only, "
            "never a bearer token.",
        )


async def current_account(
    request: Request,
    bearer: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> AsyncIterator[Account]:
    """The Bearer PAT when Authorization is present — present-but-empty still
    challenges — else the session identity."""
    if "Authorization" in request.headers:
        account = resolve_pat_account(bearer)
    else:
        account = await _resolve_operator(request)
        if not account:
            raise HTTPException(
                status_code=409,
                detail="No operator account exists yet — connect a harness to finish setup.",
            )
    token = current_account_id.set(account.id)
    try:
        yield account
    finally:
        # The actor must not leak into whatever runs on this task next.
        current_account_id.reset(token)


async def current_session_account(request: Request) -> AsyncIterator[Account]:
    """The signed-in human, never a bearer — a token cannot manage
    capabilities. Identity re-asserts per request; no session state."""
    _require_no_bearer(request)
    account = await _resolve_operator(request)
    if not account:
        raise HTTPException(
            status_code=409,
            detail="No operator account exists yet — connect a harness to finish setup.",
        )
    token = current_account_id.set(account.id)
    try:
        yield account
    finally:
        current_account_id.reset(token)


async def current_session_or_setup(request: Request) -> AsyncIterator[Account | None]:
    """The signed-in human; None during none/zero setup, where the first
    completed connection creates the operator."""
    _require_no_bearer(request)
    account = await _resolve_operator(request)
    token = current_account_id.set(account.id if account else None)
    try:
        yield account
    finally:
        current_account_id.reset(token)


async def current_account_or_setup(
    request: Request,
    bearer: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> AsyncIterator[Account | None]:
    """PAT-first identity that reads none/zero setup as None instead of
    refusing — ``/api/auth/me`` only."""
    if "Authorization" in request.headers:
        account = resolve_pat_account(bearer)
    else:
        account = await _resolve_operator(request)
    token = current_account_id.set(account.id if account else None)
    try:
        yield account
    finally:
        current_account_id.reset(token)
