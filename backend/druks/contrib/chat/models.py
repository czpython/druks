from datetime import datetime

from sqlalchemy import ForeignKey, select
from sqlalchemy.orm import Mapped, mapped_column

from druks.contrib.chat.enums import Autonomy, Role
from druks.contrib.chat.schemas import ConversationSummary
from druks.db import Base, StoredSubject, db_session


class Conversation(StoredSubject):
    __tablename__ = "chat_conversations"

    # id: the integer subject key inherited from StoredSubject; the class name
    # derives subject_type "conversation".
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    title: Mapped[str]
    # Autonomy is a String column driven by this app's closed StrEnum, not a
    # native PG enum: the modes stay in code and a rename never needs ALTER TYPE.
    autonomy: Mapped[str] = mapped_column(default=Autonomy.PROPOSE)
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    @classmethod
    async def create(
        cls,
        *,
        account_id: str,
        title: str,
        autonomy: Autonomy = Autonomy.PROPOSE,
    ) -> "Conversation":
        session = db_session()
        conversation = cls(account_id=account_id, title=title, autonomy=autonomy)
        session.add(conversation)
        await session.flush()
        return conversation

    @classmethod
    async def get(cls, conversation_id: int) -> "Conversation | None":
        return await db_session().get(cls, conversation_id)

    @classmethod
    async def list_for_account(cls, account_id: str) -> list["Conversation"]:
        """This account's threads, newest first. A conversation belongs to one
        operator; another account's list never includes it."""
        statement = (
            select(cls)
            .where(cls.account_id == account_id)
            .order_by(cls.created_at.desc(), cls.id.desc())
        )
        return list(await db_session().scalars(statement))

    async def add_message(self, *, role: Role, body: str) -> "Message":
        return await Message.create(conversation_id=self.id, role=role, body=body)

    async def list_messages(self) -> list["Message"]:
        return await Message.list_for_conversation(self.id)

    def get_summary(self) -> ConversationSummary:
        return ConversationSummary.model_validate(self)

    @classmethod
    async def list_summaries(cls, account_id: str | None) -> list[ConversationSummary]:
        """This account's threads. A missing caller is not a shared board —
        conversations are per-account, so the list is empty."""
        if account_id:
            return [
                conversation.get_summary()
                for conversation in await cls.list_for_account(account_id)
            ]
        return []


class Message(Base):
    __tablename__ = "chat_messages"

    # A row, not an event and not a StoredSubject: events stay facts about what
    # happened, while a message is the thread the conversation reads back in
    # order. Issues' ``Comment`` is the same shape.
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("chat_conversations.id"))
    role: Mapped[str]
    body: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    @classmethod
    async def create(cls, *, conversation_id: int, role: Role, body: str) -> "Message":
        session = db_session()
        message = cls(conversation_id=conversation_id, role=role, body=body)
        session.add(message)
        await session.flush()
        return message

    @classmethod
    async def list_for_conversation(cls, conversation_id: int) -> list["Message"]:
        """The thread, oldest first — a conversation reads down. A conversation
        nobody has spoken on is an empty list, never None."""
        statement = (
            select(cls)
            .where(cls.conversation_id == conversation_id)
            .order_by(cls.created_at, cls.id)
        )
        return list(await db_session().scalars(statement))
