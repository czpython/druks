from sqlalchemy.ext.asyncio import AsyncSession

from druks.chat.channels.base import Channel
from druks.chat.enums import ConversationSource
from druks.chat.exceptions import ChannelHasNoThreadsError
from druks.chat.models import Conversation, Message

from .constants import THREAD_MESSAGES
from .services import Waha


def get_source_id(message_id: str) -> str:
    """A message's id at WhatsApp. WAHA's id reads {fromMe}_{chat}_{id}, followed by
    _{participant} in a group, and Druks records a reply it sends by the plain id."""
    return message_id.split("_")[2]


def get_sender_name(message: dict) -> str:
    """The name that a message's sender shows. NOWEB keeps it as pushName, and GOWS as
    Info.PushName."""
    raw = message["_data"]
    return raw.get("pushName") or raw.get("Info", {}).get("PushName") or ""


def get_phone(chat_id: str) -> str:
    """The phone number in a chat ID. A LID holds none."""
    if chat_id.endswith("@c.us"):
        return "+" + chat_id.removesuffix("@c.us")
    return ""


class WhatsAppChannel(Channel):
    name = ConversationSource.WHATSAPP
    service = Waha
    has_readable_threads = True

    @classmethod
    async def send_reply(
        cls, session: AsyncSession, conversation: Conversation, reply: Message
    ) -> None:
        """Send a reply to the person who wrote. Druks records the reply's WhatsApp id
        before the send, so the copy that WAHA reports back is known as Druks's own."""
        is_self_chat = conversation.user_id == conversation.connection.identity["user_id"]
        text = f"[Druks] {reply.answer}" if is_self_chat else reply.answer
        client = await Waha.get_client(session, conversation.connection)
        reply.source_id = await client.new_message_id()
        await session.commit()
        chat_id = conversation.thread_id or conversation.user_id
        await client.send_text(chat_id, text, message_id=reply.source_id)

    @classmethod
    async def read_thread(cls, session: AsyncSession, conversation: Conversation) -> list[dict]:
        """The group's newest messages, oldest first, with who wrote each one. A reply is
        this conversation's by its recorded WhatsApp id."""
        if not conversation.thread_id:
            raise ChannelHasNoThreadsError("A direct chat has no thread to read.")
        client = await Waha.get_client(session, conversation.connection)
        messages = await client.get_messages(conversation.thread_id, limit=THREAD_MESSAGES)
        own_replies = await conversation.list_reply_source_ids(session)
        return [
            {
                "ts": message["timestamp"],
                "user_id": message.get("participant") or "",
                "user_name": get_sender_name(message),
                "text": message["body"] or "",
                "is_from_you": get_source_id(message["id"]) in own_replies,
                "is_from_user": message.get("participant") == conversation.user_id,
            }
            for message in reversed(messages)
        ]

    @classmethod
    async def start_conversation(
        cls, session: AsyncSession, conversation: Conversation, *, phone: str, name: str
    ) -> Conversation:
        """A chat with the WhatsApp account on a phone number, under the conversation's
        account. Only an operator calls it, and an operator's WhatsApp chats are on paired
        numbers, so an open number never starts a chat."""
        client = await Waha.get_client(session, conversation.connection)
        chat_id = await client.get_chat_id_for_phone(phone)
        return await Conversation.get_or_create_for_user(
            session,
            conversation.connection,
            conversation.account_id,
            source=ConversationSource.WHATSAPP,
            user_id=chat_id,
            user_name=name,
            user_phone=get_phone(chat_id),
            thread_id="",
        )
