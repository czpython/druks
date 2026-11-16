from fastapi import APIRouter, Depends, HTTPException, Query

from druks.accounts.dependencies import current_account
from druks.accounts.models import Account
from druks.api.dependencies import SessionDep
from druks.notifications.exceptions import (
    AlreadyAcknowledgedError,
    AnswerNotAllowedError,
    InvalidChoiceError,
    StaleRoundError,
    UnknownTokenError,
)
from druks.notifications.models import Notification
from druks.notifications.schemas import (
    ChannelResponse,
    NotificationResponse,
    RespondRequest,
)
from druks.notifications.services import list_notification_channels, respond_to_notification
from druks.teams.dependencies import require_notification_access

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

# Public ingress, token-authenticated — mounts beside the webhooks.
external_router = APIRouter(prefix="/_external/notifications", tags=["notifications"])


@router.get("/channels", response_model=list[ChannelResponse])
async def list_channels() -> list[dict]:
    return await list_notification_channels()


@router.get("", response_model=list[NotificationResponse])
async def list_notifications(
    session: SessionDep,
    account: Account = Depends(current_account),
    limit: int = Query(50, ge=1, le=500),
) -> list[Notification]:
    return await Notification.list_for_account(
        session, account.id, kinds=await account.list_usable_kinds(), limit=limit
    )


@router.post("/read", status_code=204)
async def mark_notifications_read(
    session: SessionDep, account: Account = Depends(current_account)
) -> None:
    await Notification.mark_all_read(session, account.id)


# Declared after the /channels route: declaration order is match order, so the id match
# can't swallow it.
@router.get(
    "/{notification_id}",
    response_model=NotificationResponse,
    dependencies=[Depends(require_notification_access)],
)
async def get_notification(
    session: SessionDep, notification_id: str, account: Account = Depends(current_account)
) -> Notification:
    notification = await session.get(Notification, notification_id)
    # A person's notification is theirs only. Another account finds no such notification.
    if not notification or notification.account_id not in (None, account.id):
        raise HTTPException(status_code=404, detail=f"Notification {notification_id!r} not found")
    return notification


@external_router.post("/{token}/respond", status_code=204)
async def respond(session: SessionDep, token: str, body: RespondRequest) -> None:
    try:
        await respond_to_notification(
            session,
            token,
            {"control": body.control, "answers": body.answers, "note": body.note},
            user_id=None,
            account_id=None,
        )
    except UnknownTokenError as error:
        # 404 without echoing: the token is the capability.
        raise HTTPException(status_code=404, detail="unknown token") from error
    except AnswerNotAllowedError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except (AlreadyAcknowledgedError, StaleRoundError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except InvalidChoiceError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
