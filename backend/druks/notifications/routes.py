from typing import Annotated

import apprise
from fastapi import APIRouter, Body, HTTPException, Query

from druks.database import db_session
from druks.notifications.exceptions import (
    AlreadyAcknowledgedError,
    InvalidChoiceError,
    StaleRoundError,
    UnknownTokenError,
)
from druks.notifications.models import Destination, Notification
from druks.notifications.schemas import (
    CreateDestinationRequest,
    DestinationResponse,
    NotificationResponse,
    RespondRequest,
)
from druks.notifications.services import respond_to_notification

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

# Public ingress, token-authenticated — mounts beside the webhooks.
external_router = APIRouter(prefix="/_external/notifications", tags=["notifications"])


@router.get("/destinations", response_model=list[DestinationResponse])
async def list_destinations() -> list[Destination]:
    return await Destination.list_all()


@router.post("/destinations", response_model=DestinationResponse)
async def create_destination(body: CreateDestinationRequest) -> Destination:
    if not body.name.strip():
        raise HTTPException(status_code=422, detail="Destination needs a name.")
    if await Destination.get_for_name(body.name):
        raise HTTPException(
            status_code=409, detail=f"Destination {body.name!r} already exists; remove it first."
        )
    url = body.url.get_secret_value()
    if not apprise.Apprise().add(url):
        # The same offline parse delivery performs, run while the operator is
        # still here to fix the typo — instead of at the first failed send.
        raise HTTPException(
            status_code=422, detail="URL is not a recognized notification destination."
        )
    return await Destination.create(name=body.name, kind=body.kind.value, url=url)


@router.patch("/destinations/{destination_id}", response_model=DestinationResponse)
async def set_destination_enabled(
    destination_id: str, is_enabled: Annotated[bool, Body(embed=True)]
) -> Destination:
    destination = await Destination.get(destination_id)
    if not destination:
        raise HTTPException(status_code=404, detail=f"Destination {destination_id!r} not found")
    destination.is_enabled = is_enabled
    await db_session().flush()
    return destination


@router.delete("/destinations/{destination_id}", status_code=204)
async def delete_destination(destination_id: str) -> None:
    destination = await Destination.get(destination_id)
    if not destination:
        raise HTTPException(status_code=404, detail=f"Destination {destination_id!r} not found")
    await destination.delete()


@router.get("", response_model=list[NotificationResponse])
async def list_notifications(limit: int = Query(50, ge=1, le=500)) -> list[Notification]:
    return await Notification.list_recent(limit)


# Declared after the /destinations routes: declaration order is match order,
# so the id match can't swallow them.
@router.get("/{notification_id}", response_model=NotificationResponse)
async def get_notification(notification_id: str) -> Notification:
    notification = await Notification.get(notification_id)
    if not notification:
        raise HTTPException(status_code=404, detail=f"Notification {notification_id!r} not found")
    return notification


@external_router.post("/{token}/respond", status_code=204)
async def respond(token: str, body: RespondRequest) -> None:
    # CorruptCorrelationError deliberately propagates: a run_id with no run is data
    # corruption, so it must surface as a logged 500, never a silent 404.
    try:
        await respond_to_notification(
            token, {"control": body.control, "answers": body.answers, "note": body.note}
        )
    except UnknownTokenError as error:
        # 404 without echoing: the token is the capability.
        raise HTTPException(status_code=404, detail="unknown token") from error
    except (AlreadyAcknowledgedError, StaleRoundError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except InvalidChoiceError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
