from collections.abc import Awaitable, Callable

from fastapi import Depends, Request

from druks.accounts.dependencies import current_account, current_session_account
from druks.accounts.models import Account
from druks.api.dependencies import SessionDep
from druks.apps import App
from druks.apps.loader import get_app
from druks.apps.registry import apps, workflows
from druks.chat.bots.service import get_bot_connection
from druks.durable.models import AgentCall, Artifact, Run
from druks.exceptions import ObjectNotFound, PermissionDenied
from druks.files.models import FileRecord
from druks.notifications.models import Notification


def require_app_access(app: type[App]) -> Callable[..., Awaitable[None]]:
    """The dependency in front of one app: the caller must be able to use it."""

    async def require_access(account: Account = Depends(current_account)) -> None:
        if not await account.can_use_app(app):
            raise PermissionDenied(f"{account.username} cannot use the app {app.name}.")

    return require_access


async def require_named_app_access(app: str, account: Account = Depends(current_account)) -> None:
    """The caller must be able to use the app that the ``app`` query parameter names. An
    app that is not installed answers 404."""
    try:
        named = get_app(app)
    except KeyError as error:
        raise ObjectNotFound("app", {"name": app}) from error
    if not await account.can_use_app(named):
        raise PermissionDenied(f"{account.username} cannot use the app {app}.")


# Each dependency below finds the app in the request path. A path parameter in its own
# signature would replace the route's, and the description that a tool shows for it.
# A thing of an app that is not installed (removed, or `enabled = false`) answers 404.


async def require_run_access(
    request: Request, session: SessionDep, account: Account = Depends(current_account)
) -> None:
    """The caller must be able to use the run's app. An unknown run is the route's to answer."""
    run = await session.get(Run, request.path_params["run"])
    if run:
        workflow = workflows.get(run.kind)
        if not workflow:
            raise ObjectNotFound("run", {"id": run.id})
        app = get_app(workflow.app)
        if not await account.can_use_app(app):
            raise PermissionDenied(f"{account.username} cannot use the app {app.name}.")


async def require_call_access(
    request: Request, session: SessionDep, account: Account = Depends(current_account)
) -> None:
    """The caller must be able to use the app of the call's run. An unknown call is the
    route's to answer."""
    call = await session.get(AgentCall, request.path_params["call"])
    if call:
        workflow = workflows.get(call.run.kind)
        if not workflow:
            raise ObjectNotFound("agent call", {"id": call.id})
        app = get_app(workflow.app)
        if not await account.can_use_app(app):
            raise PermissionDenied(f"{account.username} cannot use the app {app.name}.")


async def require_artifact_access(
    request: Request, session: SessionDep, account: Account = Depends(current_account)
) -> None:
    """The caller must be able to use the app of the artifact's call. An unknown artifact
    is the route's to answer."""
    artifact = await session.get(Artifact, request.path_params["artifact_id"])
    if artifact:
        call = await AgentCall.get(session, artifact.agent_call_id)
        workflow = workflows.get(call.run.kind)
        if not workflow:
            raise ObjectNotFound("artifact", {"id": artifact.id})
        app = get_app(workflow.app)
        if not await account.can_use_app(app):
            raise PermissionDenied(f"{account.username} cannot use the app {app.name}.")


async def require_file_access(
    request: Request, session: SessionDep, account: Account = Depends(current_account)
) -> None:
    """The caller must be able to use the file's app. An unknown file is the route's to
    answer."""
    record = await session.get(FileRecord, request.path_params["file_id"])
    if record:
        try:
            app = get_app(record.app)
        except KeyError as error:
            raise ObjectNotFound("file", {"id": record.id}) from error
        if not await account.can_use_app(app):
            raise PermissionDenied(f"{account.username} cannot use the app {app.name}.")


async def require_notification_access(
    request: Request, session: SessionDep, account: Account = Depends(current_account)
) -> None:
    """The caller must be able to use the app of the notification's run. An unknown
    notification is the route's to answer, and one with no run passes. A run of an app
    that is not installed answers as an unknown notification."""
    notification = await session.get(Notification, request.path_params["notification_id"])
    if notification and notification.run_id:
        run = await session.get(Run, notification.run_id)
        workflow = workflows.get(run.kind)
        app = apps.get(workflow.app) if workflow else None
        if not app:
            raise ObjectNotFound("notification", {"id": notification.id})
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
        try:
            app = get_app(connection.identity["app"])
        except KeyError as error:
            raise ObjectNotFound("number", {"id": connection.id}) from error
        if not await account.can_use_app(app):
            raise PermissionDenied(f"{account.username} cannot use the app {app.name}.")
