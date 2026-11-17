import logging
from datetime import datetime
from functools import partial

from dbos import DBOS, EnqueueOptions, StepOptions
from dbos._error import DBOSMaxStepRetriesExceeded
from sqlalchemy.ext.asyncio import AsyncSession

from druks.accounts.models import Account
from druks.chat.models import Conversation
from druks.core.services import Slack
from druks.durable.constants import NOTIFICATIONS_QUEUE
from druks.durable.engine import enqueue_in_transaction, step_session
from druks.notifications import exceptions
from druks.notifications.constants import CLOSE_WORKFLOW, CLOSED_MESSAGE, SEND_WORKFLOW
from druks.notifications.datastructures import NotificationState
from druks.notifications.delivery import deliver
from druks.notifications.models import Notification
from druks.secrets.datastructures import Audience
from druks.secrets.models import VaultSecret
from druks.services.exceptions import ServiceNotConnectedError
from druks.settings import get_settings

logger = logging.getLogger(__name__)

# Delivery retries on its own schedule, fully decoupled from run lifecycles: a
# flaky provider endpoint must never retry (or wedge) a run's steps, so the
# outbox gets its own queue + workflow instead of riding RUN_QUEUE. The
# workflow must exist before DBOS.launch(); the queue is registered after.

_SEND_RETRIES: StepOptions = {"retries_allowed": True, "max_attempts": 5}


@DBOS.workflow(name=SEND_WORKFLOW)
async def send_notification(notification_id: str) -> None:
    async def _send() -> None:
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
                channels = await list_account_channels(session, notification.account_id)
                if not channels:
                    # The bell has the notification, so it is delivered with no Slack message.
                    await (await session.get(Notification, notification_id)).mark_delivered()
                    return
                client = await Slack.get_client()
            post = partial(
                deliver,
                client,
                body=notification.body,
                actions=notification.actions,
                token=notification.correlation_token,
                deep_link=notification.deep_link,
            )
            try:
                posted = await post(channels[0])
            except exceptions.ChannelUnavailableError:
                # The next channel takes a notification that the first one refuses.
                if not channels[1:]:
                    raise
                posted = await post(channels[1])
            message_id = f"{posted['channel']}:{posted['ts']}"
        # Kept so that Druks can close the message when its question ends without a click.
        async with step_session() as session:
            await (await session.get(Notification, notification_id)).mark_delivered(message_id)

    try:
        await DBOS.run_step_async(
            StepOptions(name="notifications.send.deliver", **_SEND_RETRIES), _send
        )
    except Exception as error:
        if isinstance(error, DBOSMaxStepRetriesExceeded):
            error = error.errors[-1]
        # An unexpected error's text can quote the request; the class name alone is
        # always safe.
        reason = (
            str(error)
            if isinstance(error, (exceptions.NotificationError, ServiceNotConnectedError))
            else type(error).__name__
        )

        async def _mark_failed() -> None:
            async with step_session() as session:
                await (await session.get(Notification, notification_id)).mark_failed(reason)

        # Terminal: record the failure and return normally — re-raising would
        # put the workflow into perpetual DBOS recovery for a dead endpoint.
        await DBOS.run_step_async(StepOptions(name="notifications.send.mark_failed"), _mark_failed)


async def notify_account(
    session: AsyncSession, account_id: str, *, reason: str, body: str, path: str
) -> None:
    """Make a notification for the account and queue its send, in the session's
    transaction. The notification links to the dashboard page at ``path``."""
    endpoint = get_settings().urls.endpoint.rstrip("/")
    notification = await Notification.create(
        session,
        account_id=account_id,
        reason=reason,
        body=body,
        deep_link=f"{endpoint}{path}" if endpoint else None,
    )
    options: EnqueueOptions = {
        "workflow_name": SEND_WORKFLOW,
        "queue_name": NOTIFICATIONS_QUEUE,
        "workflow_id": f"{SEND_WORKFLOW}:{notification.id}",
    }
    await enqueue_in_transaction(session, options, notification.id)


async def list_account_channels(session: AsyncSession, account_id: str) -> list[str]:
    """Where the bot posts an account's notification, in order: the channel that the
    person chose, then their Slack user, which opens the bot's DM."""
    account = await session.get(Account, account_id)
    sign_ins = await VaultSecret.list_account_connections(
        session, Audience.service(Slack.slug), account_id
    )
    channels = []
    if account.notification_channel_id:
        channels.append(account.notification_channel_id)
    if sign_ins:
        channels.append(sign_ins[0].identity["subject"])
    return channels


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
                except (exceptions.DeliveryError, ServiceNotConnectedError) as error:
                    logger.warning("notification %s kept its buttons: %s", notification.id, error)


async def remove_buttons(session: AsyncSession, notification: Notification) -> None:
    """Take the buttons off the notification's posted message, and say that its question
    is closed. A conversation's channel edits its own message. An account's message
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
