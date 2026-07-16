from typing import Annotated

from fastapi import Depends, HTTPException, Request

from druks.accounts import sessions
from druks.accounts.models import Account


async def resolve_session_account(request: Request) -> Account | None:
    """The session cookie's account, or None; drops a session whose account
    is gone."""
    token = request.cookies.get(sessions.SESSION_COOKIE)
    if not token:
        return
    account_id = await sessions.resolve_session(token)
    if not account_id:
        return
    account = Account.get(account_id)
    if not account:
        await sessions.drop_session(token)
        return
    return account


async def current_account(request: Request) -> Account:
    """The signed-in account, else 401."""
    account = await resolve_session_account(request)
    if not account:
        raise HTTPException(status_code=401, detail="Sign in to use this API.")
    return account


# Handler-parameter form of the gate, for routes that also read the account
# (e.g. to attribute a workflow start).
CurrentAccountDep = Annotated[Account, Depends(current_account)]
