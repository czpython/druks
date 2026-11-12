from collections.abc import Awaitable, Callable

from fastapi import Depends, Request

from druks.accounts.dependencies import current_account, current_session_account
from druks.accounts.models import Account
from druks.api.dependencies import SessionDep
from druks.apps import App
from druks.apps.loader import get_app
from druks.apps.registry import workflows
from druks.chat.bots.service import get_bot_connection
from druks.durable.models import Run
from druks.exceptions import PermissionDenied


def app_access(app: type[App]) -> Callable[..., Awaitable[None]]:
    """The gate in front of one app: the caller must be able to use it."""

    async def access(account: Account = Depends(current_account)) -> None:
        if not await account.can_use_app(app):
            raise PermissionDenied(f"{account.username} cannot use the app {app.name}.")

    return access


# Each gate below finds the app in the request path. A path parameter in its own
# signature would replace the route's, and the description that a tool shows for it.


async def app_access_for_run(
    request: Request, session: SessionDep, account: Account = Depends(current_account)
) -> None:
    """The gate of the app that a run belongs to. An unknown run is the route's to answer."""
    run = await session.get(Run, request.path_params["run"])
    if run:
        await app_access(get_app(workflows.get(run.kind).app))(account)


async def app_access_for_schedule(
    request: Request, account: Account = Depends(current_account)
) -> None:
    """The gate of the app that declares a schedule. An unknown kind is the route's to answer."""
    workflow = workflows.get(request.path_params["kind"])
    if workflow:
        await app_access(get_app(workflow.app))(account)


async def app_access_for_pairing(
    request: Request, session: SessionDep, account: Account = Depends(current_session_account)
) -> None:
    """The gate of the app that a number talks to. An unknown number is the route's to answer."""
    connection = await get_bot_connection(session, request.path_params["connection_id"])
    if connection:
        await app_access(get_app(connection.identity["app"]))(account)
