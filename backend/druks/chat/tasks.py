import logging

from druks.db import db_session
from druks.workflows import task

from .models import Reminder
from .service import publish

logger = logging.getLogger(__name__)


@task(every="* * * * *")
async def send_due_reminders() -> None:
    """Send each due reminder as the Bot's message in its conversation."""
    session = db_session()
    messages = [
        (reminder.conversation, await reminder.create_message(session))
        for reminder in await Reminder.list_due(session)
    ]
    # Saving the messages before the sends releases the locks, and a send that fails
    # cannot send a reminder twice.
    await session.commit()
    for conversation, message in messages:
        if conversation.channel:
            try:
                await conversation.channel.send_reply(session, conversation, message)
            except Exception:
                # One source that refuses a message must not hold back the others.
                logger.exception("Reminder message %s did not reach its source.", message.id)
        await publish(conversation.id, {"type": "messages"})
