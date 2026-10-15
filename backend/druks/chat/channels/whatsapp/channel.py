from sqlalchemy.ext.asyncio import AsyncSession

from druks.chat.channels.base import Channel
from druks.chat.enums import ConversationSource
from druks.chat.models import Conversation, Message

from .services import Waha


class WhatsAppChannel(Channel):
    name = ConversationSource.WHATSAPP
    service = Waha

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
