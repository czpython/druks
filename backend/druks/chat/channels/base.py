from typing import ClassVar

from sqlalchemy.ext.asyncio import AsyncSession

from druks.apps.registry import channels
from druks.chat.exceptions import ChannelHasNoThreadsError, ChannelStartsNoConversationsError
from druks.chat.models import Conversation, Message
from druks.services import Service


class Channel:
    """A way people reach a Bot. It is live while its service is connected. Its name
    is the source of the conversations that arrive through it."""

    name: ClassVar[str]
    service: ClassVar[type[Service]]
    # Whether chat runs agent turns on the channel's conversations. False when something
    # outside chat answers the people on the channel.
    has_turns: ClassVar[bool] = True
    # Whether chat_read_thread reads the channel's threads, where other people write too.
    has_readable_threads: ClassVar[bool] = False
    # The reaction that acknowledges a person's message, as the channel names it.
    reaction_ack: ClassVar[str]

    def __init_subclass__(cls) -> None:
        super().__init_subclass__()
        channels.register(cls)

    @classmethod
    async def get_prompt_context(cls, session: AsyncSession, conversation: Conversation) -> dict:
        """Facts about where the conversation lives, for the Bot's prompt."""
        return {}

    @classmethod
    async def send_reply(
        cls, session: AsyncSession, conversation: Conversation, reply: Message
    ) -> None:
        """Send the agent's reply to where the conversation lives."""
        raise NotImplementedError

    @classmethod
    async def acknowledge(
        cls, session: AsyncSession, conversation: Conversation, message: Message
    ) -> None:
        """Show the person that the agent has their message."""
        raise NotImplementedError

    @classmethod
    async def read_thread(cls, session: AsyncSession, conversation: Conversation) -> list[dict]:
        """The conversation's thread at its source, oldest first."""
        raise ChannelHasNoThreadsError(f"A {cls.name} conversation has no thread to read.")

    @classmethod
    async def download_file(
        cls, session: AsyncSession, conversation: Conversation, file_id: str
    ) -> tuple[bytes, str, str]:
        """A file on the conversation's thread, by the id its source gave it, as its
        bytes, name, and content type."""
        raise ChannelHasNoThreadsError(f"A {cls.name} conversation has no thread files to read.")

    @classmethod
    async def start_conversation(
        cls, session: AsyncSession, conversation: Conversation, *, phone: str, name: str
    ) -> Conversation:
        """A direct chat with a person on the conversation's connection, under the
        conversation's account. Nothing is sent."""
        raise ChannelStartsNoConversationsError(
            f"A {cls.name} conversation cannot start a chat with a person."
        )
