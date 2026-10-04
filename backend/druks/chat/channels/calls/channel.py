from sqlalchemy import Row, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from druks.chat.channels.base import Channel
from druks.chat.enums import ConversationSource
from druks.chat.models import Conversation, Message

from .services import Voice


class CallsChannel(Channel):
    name = ConversationSource.CALL
    service = Voice
    has_turns = False

    @classmethod
    async def list_calls(cls, session: AsyncSession, number_id: str) -> list[Row]:
        """A number's calls, newest first, each with the time of its last line."""
        # A run's outcome can arrive after the call ends, so only the lines count.
        is_line = (Message.conversation_id == Conversation.id) & ~Message.is_internal
        last_line_at = func.coalesce(func.max(Message.created_at), Conversation.created_at)
        calls = await session.execute(
            select(
                Conversation.id,
                Conversation.user_phone.label("caller"),
                Conversation.created_at,
                last_line_at.label("last_line_at"),
            )
            .outerjoin(Message, is_line)
            .where(Conversation.connection_id == number_id)
            .group_by(Conversation.id)
            .order_by(Conversation.created_at.desc(), Conversation.id.desc())
        )
        return calls.all()

    @classmethod
    async def list_lines(cls, session: AsyncSession, call_id: str) -> list[Row]:
        """A call's lines, in the order they were said."""
        # A caller line holds its words in the transcript, and an assistant line in the body.
        words = func.coalesce(func.nullif(Message.transcript, ""), Message.body)
        lines = await session.execute(
            select(Message.id, Message.role, words.label("text"))
            .where(Message.conversation_id == call_id, ~Message.is_internal)
            .order_by(Message.source_id)
        )
        return lines.all()
