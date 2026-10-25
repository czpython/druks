from dbos import DBOS, StepOptions
from dbos._error import DBOSMaxStepRetriesExceeded

from druks.core.services import Slack
from druks.durable.engine import step_session
from druks.notifications.datastructures import NotificationState
from druks.notifications.delivery import deliver
from druks.notifications.exceptions import NotificationError
from druks.notifications.models import Destination, Notification
from druks.services.exceptions import ServiceNotConnectedError

# Delivery retries on its own schedule, fully decoupled from run lifecycles: a
# flaky provider endpoint must never retry (or wedge) a run's steps, so the
# outbox gets its own queue + workflow instead of riding RUN_QUEUE. The
# workflow must exist before DBOS.launch(); the queue is registered after.

_SEND_RETRIES: StepOptions = {"retries_allowed": True, "max_attempts": 5}


@DBOS.workflow(name="notifications.send")
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
            destination = await session.get(Destination, notification.destination_id)
            # deliver() runs after this session closes; expunge keeps the
            # loaded destination readable as a detached row — an attribute
            # touched past the close can't lazy-load under the async session.
            session.expunge(destination)
            client = await Slack.get_client()
            body = notification.body
            actions = notification.actions
            token = notification.correlation_token
            deep_link = notification.deep_link
            notification.attempts += 1
        await deliver(client, destination, body, actions=actions, token=token, deep_link=deep_link)
        async with step_session() as session:
            await (await session.get(Notification, notification_id)).mark_delivered()

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
            if isinstance(error, (NotificationError, ServiceNotConnectedError))
            else type(error).__name__
        )

        async def _mark_failed() -> None:
            async with step_session() as session:
                await (await session.get(Notification, notification_id)).mark_failed(reason)

        # Terminal: record the failure and return normally — re-raising would
        # put the workflow into perpetual DBOS recovery for a dead endpoint.
        await DBOS.run_step_async(StepOptions(name="notifications.send.mark_failed"), _mark_failed)
