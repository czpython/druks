from datetime import datetime

from sqlalchemy import ForeignKey, Index, func, select, text, update
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, column_property, mapped_column, relationship

from druks.accounts.enums import AccountKind
from druks.accounts.models import Account
from druks.core.models import Uuid7Pk, uuid7_str
from druks.durable.dbos_state import workflow_status
from druks.files import FileField
from druks.files.datastructures import File
from druks.files.models import FileRecord
from druks.models import Base
from druks.secrets.models import VaultSecret

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
    # Druks wrote it for the agent. It starts a turn and never reaches the source.
    is_internal: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    # The message's id at its source. Druks records a reply's id before it sends the
    # reply, so the source's copy of it is known as Druks's own.
    source_id: Mapped[str | None] = mapped_column(unique=True)
    file: Mapped[File | None] = FileField()
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    delivered_at: Mapped[datetime | None]

    @classmethod
    async def get_for_source_id(cls, session: AsyncSession, source_id: str) -> "Message | None":
        return await session.scalar(select(cls).where(cls.source_id == source_id))

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
    __table_args__ = (
        Index("chat_conversations_account_idx", "account_id", "created_at"),
        Index(
            "chat_conversations_user_idx",
            "connection_id",
            "account_id",
            "user_id",
            unique=True,
            postgresql_where=text("connection_id IS NOT NULL"),
        ),
    )

    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    account: Mapped[Account] = relationship(lazy="joined")
    source: Mapped[str] = mapped_column(default=ConversationSource.WEB)
    # The channel connection that the conversation arrives on, such as a linked number.
    connection_id: Mapped[str | None] = mapped_column(ForeignKey("vault.id", ondelete="RESTRICT"))
    # Loaded in its own query: a join would hand the vault's encrypted column a NULL.
    connection: Mapped[VaultSecret | None] = relationship(lazy="selectin")
    # The person who writes, as the source names them.
    user_id: Mapped[str | None]
    user_name: Mapped[str] = mapped_column(default="", server_default=text("''"))
    user_phone: Mapped[str] = mapped_column(default="", server_default=text("''"))
    title: Mapped[str | None]
    pinned: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    session_file: Mapped[File | None] = FileField()
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    messages: Mapped[list[Message]] = relationship(
        order_by=(Message.created_at, Message.id), viewonly=True
    )

    @property
    def admin_account_id(self) -> str | None:
        """The admin account of the app number that the conversation arrives on."""
        if self.connection and self.connection.account.kind == AccountKind.BOT:
            return self.connection.identity["admin"]["account_id"]
        return

    def is_answerable_by(self, account_id: str | None) -> bool:
        """Whether the account may answer a question that the conversation's run asks.
        On an app number, only the number's admin may."""
        if admin_account_id := self.admin_account_id:
            return account_id == admin_account_id
        return True

    @classmethod
    async def create(cls, session: AsyncSession, *, account_id: str, body: str) -> "Conversation":
        """A new conversation and its first message."""
        conversation = cls(account_id=account_id)
        session.add(conversation)
        await session.flush()
        await conversation.create_message(session, body)
        return conversation

    @classmethod
    async def get_or_create_for_user(
        cls,
        session: AsyncSession,
        connection: VaultSecret,
        account_id: str,
        *,
        source: ConversationSource,
        user_id: str,
        user_name: str,
        user_phone: str,
    ) -> "Conversation":
        """The one conversation of a person on a channel's connection, under the account
        the person routes to. It keeps the name the person shows now."""
        await session.execute(
            insert(cls)
            .values(
                id=uuid7_str(),
                account_id=account_id,
                source=source,
                connection_id=connection.id,
                user_id=user_id,
                user_name=user_name,
                user_phone=user_phone,
                created_at=Base.utc_now(),
            )
            .on_conflict_do_nothing(
                index_elements=["connection_id", "account_id", "user_id"],
                index_where=text("connection_id IS NOT NULL"),
            )
        )
        conversation = await session.scalar(
            select(cls).where(
                cls.connection_id == connection.id,
                cls.account_id == account_id,
                cls.user_id == user_id,
            )
        )
        if user_name:
            conversation.user_name = user_name
        return conversation

    async def create_message(
        self,
        session: AsyncSession,
        body: str,
        *,
        role: MessageRole = MessageRole.USER,
        reply_to: Message | None = None,
        tool_calls: list[dict] | None = None,
        is_internal: bool = False,
        source_id: str | None = None,
        file: File | None = None,
    ) -> Message:
        """A message in the conversation. The person's message starts pending."""
        message = Message(
            conversation_id=self.id,
            role=role,
            body=body,
            reply_to=reply_to.id if reply_to else None,
            state=MessageState.PENDING if role == MessageRole.USER else None,
            tool_calls=tool_calls or [],
            is_internal=is_internal,
            source_id=source_id,
            file=file,
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
                .order_by(cls.last_message_at.desc(), cls.id.desc())
            )
        )

    @classmethod
    async def list_for_connection(
        cls, session: AsyncSession, connection_id: str
    ) -> list["Conversation"]:
        return list(
            await session.scalars(
                select(cls)
                .where(cls.connection_id == connection_id)
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
        """The message whose turn comes first: the newest delivered message, which
        names the turn the agent answers now, or else the oldest pending message."""
        delivered_message = await session.scalar(
            select(Message)
            .where(Message.conversation_id == self.id, Message.state == MessageState.DELIVERED)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
        )
        return delivered_message or await session.scalar(
            select(Message)
            .where(Message.conversation_id == self.id, Message.state == MessageState.PENDING)
            .order_by(Message.created_at, Message.id)
            .limit(1)
        )

    async def list_pending_messages(self, session: AsyncSession) -> list[Message]:
        return list(
            await session.scalars(
                select(Message)
                .where(Message.conversation_id == self.id, Message.state == MessageState.PENDING)
                .order_by(Message.created_at, Message.id)
            )
        )

    async def end_turn(self, session: AsyncSession, state: MessageState) -> None:
        """Give every message of the running turn its final state."""
        await session.execute(
            update(Message)
            .where(Message.conversation_id == self.id, Message.state == MessageState.DELIVERED)
            .values(state=state)
            .execution_options(synchronize_session="fetch")
        )

    async def is_held(self, session: AsyncSession) -> bool:
        """Whether the chat's turns wait: a person answers it from the number's phone,
        or its number was removed."""
        if self.connection and not self.connection.is_live:
            return True
        return bool(await self.get_pause_id(session))

    async def get_pause_id(self, session: AsyncSession) -> str | None:
        """The open pause of the conversation, while a person answers it from the phone."""
        return await session.scalar(
            select(workflow_status.c.workflow_uuid).where(
                workflow_status.c.attributes["paused_conversation_id"].as_string() == self.id,
                workflow_status.c.status.in_(("ENQUEUED", "PENDING")),
            )
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
Conversation.last_message_at = column_property(
    func.coalesce(
        select(func.max(Message.created_at))
        .where(Message.conversation_id == Conversation.id)
        .correlate_except(Message)
        .scalar_subquery(),
        Conversation.created_at,
    )
)
Conversation.last_reply_at = column_property(
    select(func.max(Message.created_at))
    .where(Message.conversation_id == Conversation.id, Message.role == MessageRole.ASSISTANT)
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
