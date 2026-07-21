from fastapi import HTTPException, Request

from druks.accounts import sessions
from druks.accounts.exceptions import InvalidPatError
from druks.accounts.models import Account, PersonalAccessToken

_BEARER_CHALLENGE = 'Bearer realm="druks"'


async def resolve_session_account(request: Request) -> Account | None:
    token = request.cookies.get(sessions.SESSION_COOKIE)
    if not token:
        return
    account_id = await sessions.resolve_session(token)
    if not account_id:
        return
    return Account.get(account_id)


async def current_session_account(request: Request) -> Account:
    """The signed-in session's account, else 401 — the only door for PAT
    management, so a token can never manage tokens."""
    account = await resolve_session_account(request)
    if account:
        sessions.current_account_id.set(account.id)
        return account
    raise HTTPException(status_code=401, detail="Sign in to use this API.")


async def current_account(request: Request) -> Account:
    """The calling account: the Bearer personal access token when an
    Authorization header is present — never falling back to the cookie —
    else the signed-in session."""
    header = request.headers.get("Authorization")
    # Present-but-empty is still present: it must be challenged, never slide
    # to the cookie.
    if header is not None:
        scheme, _, credential = header.partition(" ")
        if scheme == "Bearer" and credential and " " not in credential:
            try:
                pat = PersonalAccessToken.authenticate(credential)
            except InvalidPatError as error:
                raise HTTPException(
                    status_code=401,
                    detail=str(error),
                    headers={"WWW-Authenticate": f'{_BEARER_CHALLENGE}, error="invalid_token"'},
                ) from error
            sessions.current_account_id.set(pat.account_id)
            return pat.account
        raise HTTPException(
            status_code=401,
            detail="Authorization must be exactly: Bearer <token>.",
            headers={"WWW-Authenticate": _BEARER_CHALLENGE},
        )
    return await current_session_account(request)
