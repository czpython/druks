from druks.db import db_session
from druks.workflows import task

from .models import Reminder
from .service import send_assistant_message


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
        await send_assistant_message(session, conversation, message)
