from contextlib import suppress
from datetime import datetime
from typing import Annotated

from dbos import DBOS
from fastapi import APIRouter, Body, Depends, HTTPException, Path
from githubkit.exception import RequestFailed
from slack_sdk.errors import SlackApiError

from druks.accounts.context import current_conversation_id
from druks.accounts.dependencies import current_account, current_session_account
from druks.accounts.models import Account
from druks.api.dependencies import SessionDep

from .channels.whatsapp.exceptions import NumberNotOnWhatsAppError, WahaError
from .enums import MessageState
from .exceptions import (
    ChannelHasNoThreadsError,
    ChannelStartsNoConversationsError,
    ChatSandboxGone,
    ReminderError,
)
from .models import Conversation, Message, Reminder
from .schemas import (
    ConversationDetailResponse,
    ConversationResponse,
    MessageResponse,
    ReminderResponse,
)
from .service import cancel_turn, deliver, publish

router = APIRouter()


@router.get(
    "/conversations",
    operation_id="list_conversations",
    tags=["agent", "bot"],
    response_model=list[ConversationResponse],
    response_model_by_alias=True,
)
async def list_conversations(
    session: SessionDep, account: Account = Depends(current_account)
) -> list[Conversation]:
    """Your conversations, newest first. thread_id is set for a group, and title names
    it. A number's admin also gets the direct chats of everyone who wrote to the number.
    A person has a direct chat only after they wrote."""
    return await Conversation.list_for_account(session, account)


@router.post(
    "/conversations",
    status_code=201,
    response_model=ConversationDetailResponse,
    response_model_by_alias=True,
)
async def create_conversation(
    session: SessionDep,
    body: Annotated[str, Body(embed=True)],
    account: Account = Depends(current_session_account),
) -> Conversation:
    if not body.strip():
        raise HTTPException(422, "Write a message.")
    conversation = await Conversation.create(session, account_id=account.id, body=body)
    await session.commit()
    await DBOS.start_workflow_async(deliver, conversation.id)
    await session.refresh(
        conversation,
        ["messages", "message_count", "active_message_id", "last_message_at"],
    )
    return conversation


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetailResponse,
    response_model_by_alias=True,
)
async def get_conversation(
    conversation_id: str, session: SessionDep, account: Account = Depends(current_session_account)
) -> Conversation:
    conversation = await Conversation.get_for_account(session, conversation_id, account)
    if not conversation:
        raise HTTPException(404, "Conversation not found.")
    await session.refresh(conversation, ["messages"])
    return conversation


@router.patch(
    "/conversations/{conversation_id}",
    response_model=ConversationResponse,
    response_model_by_alias=True,
)
async def set_conversation_pinned(
    conversation_id: str,
    session: SessionDep,
    is_pinned: Annotated[bool, Body(embed=True)],
    account: Account = Depends(current_session_account),
) -> Conversation:
    conversation = await Conversation.get_for_account(session, conversation_id, account)
    if not conversation:
        raise HTTPException(404, "Conversation not found.")
    conversation.is_pinned = is_pinned
    await session.commit()
    await session.refresh(conversation)
    return conversation


@router.post(
    "/conversations/{conversation_id}/messages",
    status_code=202,
    response_model=MessageResponse,
    response_model_by_alias=True,
)
async def create_message(
    conversation_id: str,
    session: SessionDep,
    body: Annotated[str, Body(embed=True)],
    account: Account = Depends(current_session_account),
) -> Message:
    conversation = await Conversation.get_for_account(session, conversation_id, account)
    if not conversation:
        raise HTTPException(404, "Conversation not found.")
    if not body.strip():
        raise HTTPException(422, "Write a message.")
    message = await conversation.create_message(session, body)
    await session.commit()
    await publish(conversation.id, {"type": "messages"})
    await DBOS.start_workflow_async(deliver, conversation.id)
    return message


@router.post(
    "/conversations/{conversation_id}/messages/{message_id}/retry",
    status_code=202,
    response_model=MessageResponse,
    response_model_by_alias=True,
)
async def retry_message(
    conversation_id: str,
    message_id: str,
    session: SessionDep,
    account: Account = Depends(current_session_account),
) -> Message:
    conversation = await Conversation.get_for_account(session, conversation_id, account)
    if not conversation:
        raise HTTPException(404, "Conversation not found.")
    original_message = await conversation.get_message(session, message_id)
    if not original_message:
        raise HTTPException(404, "Message not found.")
    if original_message.state not in (
        MessageState.FAILED,
        MessageState.INTERRUPTED,
        MessageState.CANCELLED,
    ):
        raise HTTPException(
            409,
            f"The message is {original_message.state}. Send again only a failed, "
            "interrupted, or cancelled message.",
        )
    message = await conversation.create_message(
        session,
        original_message.body,
        file=original_message.file,
        is_internal=original_message.is_internal,
    )
    await session.commit()
    await publish(conversation.id, {"type": "messages"})
    await DBOS.start_workflow_async(deliver, conversation.id)
    return message


@router.post("/conversations/{conversation_id}/cancel", status_code=204)
async def cancel(
    conversation_id: str,
    session: SessionDep,
    message_id: Annotated[str, Body(embed=True, alias="messageId")],
    account: Account = Depends(current_session_account),
) -> None:
    conversation = await Conversation.get_for_account(session, conversation_id, account)
    if not conversation:
        raise HTTPException(404, "Conversation not found.")
    message = await conversation.get_message(session, message_id)
    if not message:
        raise HTTPException(404, "Message not found.")
    with suppress(ChatSandboxGone):
        await cancel_turn(session, conversation, message)
    await DBOS.start_workflow_async(deliver, conversation.id)


@router.get("/thread", operation_id="read_thread", tags=["agent", "bot"])
async def read_thread(
    session: SessionDep, account: Account = Depends(current_account)
) -> list[dict]:
    """Read the thread of the conversation this call comes from: its newest 200
    messages, oldest first. Each one has ts, user_id, user_name, text, is_from_you (you
    wrote it), and is_from_user (the person you answer wrote it). A direct message has
    no thread."""
    conversation_id = current_conversation_id.get()
    if not conversation_id:
        raise HTTPException(
            409, "This tool reads a channel conversation's thread. Call it from one."
        )
    conversation = await session.get(Conversation, conversation_id)
    if not conversation.channel:
        raise HTTPException(409, "A web conversation has no thread.")
    try:
        return await conversation.channel.read_thread(session, conversation)
    except ChannelHasNoThreadsError as error:
        raise HTTPException(409, str(error)) from error
    except (SlackApiError, RequestFailed, WahaError) as error:
        raise HTTPException(502, str(error)) from error


@router.put("/thread/tag", operation_id="require_tag", status_code=204, tags=["agent"])
async def require_tag(
    session: SessionDep,
    is_required: Annotated[bool, Body(embed=True)],
    account: Account = Depends(current_account),
) -> None:
    """Require a tag in the thread of the conversation this call comes from, or stop
    requiring one. While a tag is required, the person's untagged replies in the thread
    do not reach you. Their tagged messages always do."""
    conversation_id = current_conversation_id.get()
    if not conversation_id:
        raise HTTPException(
            409, "This tool sets a channel conversation's thread. Call it from one."
        )
    conversation = await session.get(Conversation, conversation_id)
    if not conversation.thread_id:
        raise HTTPException(409, "This conversation has no thread. Every message reaches you.")
    conversation.is_tag_required = is_required
    await session.commit()


@router.post(
    "/connection/conversations",
    operation_id="start_conversation",
    status_code=201,
    tags=["agent"],
    response_model=ConversationResponse,
    response_model_by_alias=True,
)
async def start_conversation(
    session: SessionDep,
    phone: Annotated[str, Body(description="The person's phone number, with its country code.")],
    name: Annotated[str, Body(description="The person's name, as you call them.")],
    account: Account = Depends(current_account),
) -> Conversation:
    """Start a chat with a person on the number this call comes from, so your reminders
    reach them. Druks sends nothing until a reminder does."""
    conversation_id = current_conversation_id.get()
    if not conversation_id:
        raise HTTPException(409, "Call this tool from a chat with the Bot's number.")
    conversation = await session.get(Conversation, conversation_id)
    if not conversation.channel:
        raise HTTPException(409, "A web conversation has no number to start a chat on.")
    try:
        started = await conversation.channel.start_conversation(
            session, conversation, phone=phone, name=name
        )
    except ChannelStartsNoConversationsError as error:
        raise HTTPException(409, str(error)) from error
    except NumberNotOnWhatsAppError as error:
        raise HTTPException(422, str(error)) from error
    except WahaError as error:
        raise HTTPException(502, str(error)) from error
    await session.commit()
    await session.refresh(started)
    return started


@router.post(
    "/reminders",
    operation_id="create_reminder",
    status_code=201,
    tags=["agent", "bot"],
    response_model=ReminderResponse,
    response_model_by_alias=True,
)
async def create_reminder(
    session: SessionDep,
    body: Annotated[str, Body(description="The words to send, as the person wants them.")],
    conversation_id: Annotated[
        str,
        Body(
            description="Where to send: a conversation from chat_list_conversations, or a "
            "chat you start with chat_start_conversation for someone who has none. Leave it "
            "out to send in this chat."
        ),
    ] = "",
    send_at: Annotated[
        datetime | None, Body(description="When to send once. A time with no offset is local.")
    ] = None,
    delay_seconds: Annotated[
        int, Body(description='How long from now to send once, such as 300 for "in 5 minutes".')
    ] = 0,
    cron: Annotated[str, Body(description="A cron to send again and again, local time.")] = "",
    account: Account = Depends(current_account),
) -> Reminder:
    """Send a message in a conversation once, at send_at or delay_seconds from now, or
    again and again on a cron. Give exactly one of the three. Druks sends the body word
    for word at its first check after the reminder is due, and it checks every minute.
    Local time is the installation timezone. The answer's nextSendAt is local: tell the
    person that time as it is. When Druks refuses a time, ask the person rather than
    pick another, and never make a reminder to test the time."""
    conversation_id = conversation_id or current_conversation_id.get()
    if not conversation_id:
        raise HTTPException(422, "Give conversation_id: this call comes from no chat.")
    conversation = await Conversation.get_for_account(session, conversation_id, account)
    if not conversation:
        raise HTTPException(404, "Conversation not found. Choose one from chat_list_conversations.")
    try:
        reminder = Reminder.create(
            session,
            conversation,
            account.id,
            body=body,
            send_at=send_at,
            delay_seconds=delay_seconds,
            cron=cron,
        )
    except ReminderError as error:
        raise HTTPException(422, str(error)) from error
    await session.commit()
    await session.refresh(reminder)
    return reminder


@router.get(
    "/reminders",
    operation_id="list_reminders",
    tags=["agent", "bot"],
    response_model=list[ReminderResponse],
    response_model_by_alias=True,
)
async def list_reminders(
    session: SessionDep, account: Account = Depends(current_account)
) -> list[Reminder]:
    """Your reminders, newest first: each one's conversation, body, cron, and next time."""
    return await Reminder.list_for_account(session, account.id)


@router.delete(
    "/reminders/{reminder_id}",
    operation_id="cancel_reminder",
    status_code=204,
    tags=["agent", "bot"],
)
async def cancel_reminder(
    session: SessionDep,
    reminder_id: Annotated[str, Path(description="A reminder from chat_list_reminders.")],
    account: Account = Depends(current_account),
) -> None:
    """Stop one of your reminders. What it already sent stays in the conversation."""
    reminder = await Reminder.get_for_account(session, reminder_id, account.id)
    if not reminder:
        raise HTTPException(404, "Reminder not found.")
    await session.delete(reminder)
    await session.commit()
