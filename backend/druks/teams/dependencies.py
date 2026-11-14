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


def require_app_access(app: type[App]) -> Callable[..., Awaitable[None]]:
    """The dependency in front of one app: the caller must be able to use it."""

    async def require_access(account: Account = Depends(current_account)) -> None:
        if not await account.can_use_app(app):
            raise PermissionDenied(f"{account.username} cannot use the app {app.name}.")

    return require_access


# Each dependency below finds the app in the request path. A path parameter in its own
# signature would replace the route's, and the description that a tool shows for it.


async def require_run_access(
    request: Request, session: SessionDep, account: Account = Depends(current_account)
) -> None:
    """The caller must be able to use the run's app. An unknown run is the route's to answer."""
    run = await session.get(Run, request.path_params["run"])
    if run:
        app = get_app(workflows.get(run.kind).app)
        if not await account.can_use_app(app):
            raise PermissionDenied(f"{account.username} cannot use the app {app.name}.")


async def require_schedule_access(
    request: Request, account: Account = Depends(current_account)
) -> None:
    """The caller must be able to use the app that declares the schedule. An unknown kind
    is the route's to answer."""
    workflow = workflows.get(request.path_params["kind"])
    if workflow:
        app = get_app(workflow.app)
        if not await account.can_use_app(app):
            raise PermissionDenied(f"{account.username} cannot use the app {app.name}.")


async def require_pairing_access(
    request: Request, session: SessionDep, account: Account = Depends(current_session_account)
) -> None:
    """The caller must be able to use the app that the number talks to. An unknown number
    is the route's to answer."""
    connection = await get_bot_connection(session, request.path_params["connection_id"])
    if connection:
        app = get_app(connection.identity["app"])
        if not await account.can_use_app(app):
            raise PermissionDenied(f"{account.username} cannot use the app {app.name}.")
