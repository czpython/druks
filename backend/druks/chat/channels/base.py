from typing import ClassVar

from sqlalchemy.ext.asyncio import AsyncSession

from druks.apps.registry import channels
from druks.chat.exceptions import ChannelHasNoThreadsError
from druks.chat.models import Conversation, Message
from druks.services import Service


class Channel:
    """A way people reach a Bot. It is live while its service is connected. Its name
    is the source of the conversations that arrive through it."""

    name: ClassVar[str]
    service: ClassVar[type[Service]]

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
    async def read_thread(cls, session: AsyncSession, conversation: Conversation) -> list[dict]:
        """The conversation's thread at its source, oldest first."""
        raise ChannelHasNoThreadsError(f"A {cls.name} conversation has no thread to read.")
