from dbos import DBOS, Queue, StepOptions
from dbos._error import DBOSMaxStepRetriesExceeded

from druks.durable.engine import step_session
from druks.notifications.datastructures import NotificationState
from druks.notifications.delivery import deliver
from druks.notifications.exceptions import NotificationError
from druks.notifications.models import Destination, Notification

# Delivery retries on its own schedule, fully decoupled from run lifecycles: a
# flaky provider endpoint must never retry (or wedge) a run's steps, so the
# outbox gets its own queue + workflow instead of riding run_queue. Both are
# module-level so a workflow body can import and enqueue directly.
notifications_queue = Queue("druks_notifications")

_SEND_RETRIES: StepOptions = {"retries_allowed": True, "max_attempts": 5}


def _sanitized(error: Exception) -> str:
    if isinstance(error, DBOSMaxStepRetriesExceeded):
        error = error.errors[-1]
    if isinstance(error, NotificationError):
        return str(error)
    # An unexpected error's text can embed the webhook URL (httpx puts the
    # request URL in every message); the class name alone is always safe.
    return type(error).__name__


@DBOS.workflow(name="notifications.send")
async def send_notification(notification_id: str) -> None:
    async def _send() -> None:
        async with step_session() as session:
            notification = await Notification.get(notification_id)
            # A duplicate enqueue or the replay of a completed send finds the
            # row delivered and stops. At-least-once: a crash after the send but
            # before this step checkpoints re-posts once on recovery — the click
            # side is idempotent, so a duplicate message is the accepted cost.
            if notification.state == NotificationState.DELIVERED:
                return
            destination = await Destination.get(notification.destination_id)
            # deliver() runs after this session closes; expunge keeps the
            # loaded destination readable as a detached row — an attribute
            # touched past the close can't lazy-load under the async session.
            session.expunge(destination)
            body = notification.body
            actions = notification.actions
            token = notification.correlation_token
            notification.attempts += 1
        await deliver(
            destination,
            body,
            actions=actions,
            token=token,
            idempotency_key=notification_id,
        )
        async with step_session():
            await (await Notification.get(notification_id)).mark_delivered()

    try:
        await DBOS.run_step_async(
            StepOptions(name="notifications.send.deliver", **_SEND_RETRIES), _send
        )
    except Exception as error:
        reason = _sanitized(error)

        async def _mark_failed() -> None:
            async with step_session():
                await (await Notification.get(notification_id)).mark_failed(reason)

        # Terminal: record the failure and return normally — re-raising would
        # put the workflow into perpetual DBOS recovery for a dead endpoint.
        await DBOS.run_step_async(StepOptions(name="notifications.send.mark_failed"), _mark_failed)
