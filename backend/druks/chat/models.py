from datetime import datetime

from sqlalchemy import ForeignKey, Index, func, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, column_property, mapped_column, relationship

from druks.core.models import Uuid7Pk
from druks.files import FileField
from druks.files.datastructures import File
from druks.files.models import FileRecord
from druks.models import Base

from .enums import ConversationSource, MessageRole, MessageState


class Message(Base, Uuid7Pk):
    __tablename__ = "chat_messages"
    __table_args__ = (Index("chat_messages_conversation_idx", "conversation_id", "created_at"),)

    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("chat_conversations.id", ondelete="CASCADE")
    )
    role: Mapped[str] = mapped_column(default=MessageRole.USER)
    body: Mapped[str]
    # The message a reply answers.
    reply_to: Mapped[str | None] = mapped_column(ForeignKey("chat_messages.id", ondelete="CASCADE"))
    state: Mapped[str | None]
    tool_calls: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    delivered_at: Mapped[datetime | None]

    async def mark_delivered(self, session: AsyncSession) -> bool:
        """Mark the message delivered, unless a Stop cancelled it first."""
        result = await session.execute(
            update(Message)
            .where(Message.id == self.id, Message.state == MessageState.PENDING)
            .values(state=MessageState.DELIVERED, delivered_at=Base.utc_now())
            .returning(Message.id)
        )
        return bool(result.scalar_one_or_none())

    async def cancel_pending(self, session: AsyncSession) -> bool:
        """Cancel the message, unless delivery marked it delivered first."""
        result = await session.execute(
            update(Message)
            .where(Message.id == self.id, Message.state == MessageState.PENDING)
            .values(state=MessageState.CANCELLED)
            .returning(Message.id)
        )
        return bool(result.scalar_one_or_none())


class Conversation(Base, Uuid7Pk):
    __tablename__ = "chat_conversations"
    __table_args__ = (Index("chat_conversations_account_idx", "account_id", "created_at"),)

    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    source: Mapped[str] = mapped_column(default=ConversationSource.WEB)
    title: Mapped[str | None]
    session_file: Mapped[File | None] = FileField()
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    messages: Mapped[list[Message]] = relationship(
        order_by=(Message.created_at, Message.id), viewonly=True
    )

    @classmethod
    async def create(cls, session: AsyncSession, *, account_id: str, body: str) -> "Conversation":
        """A new conversation and its first message."""
        conversation = cls(account_id=account_id)
        session.add(conversation)
        await session.flush()
        await conversation.create_message(session, body)
        return conversation

    async def create_message(
        self,
        session: AsyncSession,
        body: str,
        *,
        role: MessageRole = MessageRole.USER,
        reply_to: Message | None = None,
        tool_calls: list[dict] | None = None,
    ) -> Message:
        """A message in the conversation. The person's message starts pending."""
        message = Message(
            conversation_id=self.id,
            role=role,
            body=body,
            reply_to=reply_to.id if reply_to else None,
            state=MessageState.PENDING if role == MessageRole.USER else None,
            tool_calls=tool_calls or [],
        )
        session.add(message)
        await session.flush()
        return message

    @classmethod
    async def get_for_account(
        cls, session: AsyncSession, conversation_id: str, account_id: str
    ) -> "Conversation | None":
        return await session.scalar(
            select(cls).where(cls.id == conversation_id, cls.account_id == account_id)
        )

    @classmethod
    async def list_for_account(cls, session: AsyncSession, account_id: str) -> list["Conversation"]:
        return list(
            await session.scalars(
                select(cls)
                .where(cls.account_id == account_id)
                .order_by(cls.created_at.desc(), cls.id.desc())
            )
        )

    async def get_message(self, session: AsyncSession, message_id: str) -> Message | None:
        """One of the person's messages in this conversation."""
        return await session.scalar(
            select(Message).where(
                Message.id == message_id,
                Message.conversation_id == self.id,
                Message.role == MessageRole.USER,
            )
        )

    async def get_unanswered_message(self, session: AsyncSession) -> Message | None:
        """The oldest message of the person that still waits for its reply: it is
        pending, or delivered while the agent replies."""
        return await session.scalar(
            select(Message)
            .where(
                Message.conversation_id == self.id,
                Message.state.in_((MessageState.PENDING, MessageState.DELIVERED)),
            )
            .order_by(Message.created_at, Message.id)
            .limit(1)
        )

    async def set_session_file(self, session: AsyncSession, file: File) -> None:
        """Keep the agent's latest session files; the file reaper removes the previous ones."""
        if self.session_file:
            previous = await session.get(FileRecord, self.session_file.id)
            previous.deleted_at = Base.utc_now()
        self.session_file = file


Conversation.message_count = column_property(
    select(func.count(Message.id))
    .where(Message.conversation_id == Conversation.id)
    .correlate_except(Message)
    .scalar_subquery()
)
# A delivered message is the one the agent is answering now.
Conversation.active_message_id = column_property(
    select(Message.id)
    .where(Message.conversation_id == Conversation.id, Message.state == MessageState.DELIVERED)
    .correlate_except(Message)
    .limit(1)
    .scalar_subquery()
)
