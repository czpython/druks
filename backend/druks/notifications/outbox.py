import logging
from datetime import datetime

from dbos import DBOS, StepOptions
from dbos._error import DBOSMaxStepRetriesExceeded
from sqlalchemy.ext.asyncio import AsyncSession

from druks.chat.models import Conversation
from druks.core.services import Slack
from druks.durable.engine import step_session
from druks.notifications.constants import CLOSE_WORKFLOW, CLOSED_MESSAGE
from druks.notifications.datastructures import NotificationState
from druks.notifications.delivery import deliver
from druks.notifications.exceptions import (
    DeliveryError,
    DisabledDestinationError,
    NotificationError,
)
from druks.notifications.models import Destination, Notification
from druks.services.exceptions import ServiceNotConnectedError

logger = logging.getLogger(__name__)

# Delivery retries on its own schedule, fully decoupled from run lifecycles: a
# flaky provider endpoint must never retry (or wedge) a run's steps, so the
# outbox gets its own queue + workflow instead of riding RUN_QUEUE. The
# workflow must exist before DBOS.launch(); the queue is registered after.

_SEND_RETRIES: StepOptions = {"retries_allowed": True, "max_attempts": 5}


@DBOS.workflow(name="notifications.send")
async def send_notification(notification_id: str) -> None:
    try:
        await DBOS.run_step_async(
            StepOptions(name="notifications.send.deliver", **_SEND_RETRIES),
            _send,
            notification_id,
        )
    except Exception as error:
        if isinstance(error, DBOSMaxStepRetriesExceeded):
            error = error.errors[-1]
        # An unexpected error's text can quote the request; the class name alone is
        # always safe.
        reason = (
            str(error)
            if isinstance(error, (NotificationError, ServiceNotConnectedError))
            else type(error).__name__
        )
        # Terminal: record the failure and return normally — re-raising would
        # put the workflow into perpetual DBOS recovery for a dead endpoint.
        await DBOS.run_step_async(
            StepOptions(name="notifications.send.mark_failed"),
            _mark_failed,
            notification_id,
            reason,
        )


async def _send(notification_id: str) -> None:
    async with step_session() as session:
        notification = await session.get(Notification, notification_id)
        # A duplicate enqueue or the replay of a completed send finds the
        # row delivered and stops. At-least-once: a crash after the send but
        # before this step checkpoints re-posts once on recovery — the click
        # side is idempotent, so a duplicate message is the accepted cost.
        if notification.state == NotificationState.DELIVERED:
            return
        notification.attempts += 1
    # Delivery runs after that session commits, so a refusal still counts the
    # attempt. The detached rows stay readable: sessions don't expire on commit.
    if notification.conversation_id:
        # The conversation's channel posts where it lives, and reads its grant here.
        async with step_session() as session:
            conversation = await session.get(Conversation, notification.conversation_id)
            message_id = await conversation.channel.post_buttons(
                session, conversation, notification
            )
    else:
        async with step_session() as session:
            destination = await session.get(Destination, notification.destination_id)
            client = await Slack.get_client()
        if not destination.is_enabled:
            raise DisabledDestinationError(destination.name)
        posted = await deliver(
            client,
            destination.channel_id,
            notification.body,
            actions=notification.actions,
            token=notification.correlation_token,
            deep_link=notification.deep_link,
        )
        message_id = f"{posted['channel']}:{posted['ts']}"
    # Kept so that Druks can close the message when its question ends without a click.
    async with step_session() as session:
        await (await session.get(Notification, notification_id)).mark_delivered(message_id)


async def _mark_failed(notification_id: str, reason: str) -> None:
    async with step_session() as session:
        await (await session.get(Notification, notification_id)).mark_failed(reason)


@DBOS.workflow(name=CLOSE_WORKFLOW)
async def close_notifications(run_id: str, parked_at: str) -> None:
    # The claims make a rerun redo only the messages still open, so the body needs no step.
    async with step_session() as session:
        for notification in await Notification.list_open(
            session, run_id, datetime.fromisoformat(parked_at)
        ):
            if await notification.mark_acknowledged():
                # The other messages still close when one edit fails.
                try:
                    await remove_buttons(session, notification)
                except (DeliveryError, ServiceNotConnectedError) as error:
                    logger.warning("notification %s kept its buttons: %s", notification.id, error)


async def remove_buttons(session: AsyncSession, notification: Notification) -> None:
    """Take the buttons off the notification's posted message, and say that its question
    is closed. A conversation's channel edits its own message. A destination's message
    goes through Slack, as its post did."""
    if notification.conversation_id:
        conversation = await session.get(Conversation, notification.conversation_id)
        await conversation.channel.remove_buttons(session, conversation, notification)
    else:
        room, message_ts = notification.message_id.split(":")
        await deliver(
            await Slack.get_client(),
            room,
            notification.body,
            message_ts=message_ts,
            deep_link=notification.deep_link,
            outcome=CLOSED_MESSAGE,
        )
