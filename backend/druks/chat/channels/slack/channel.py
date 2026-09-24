import json
import secrets

from dbos import DBOS
from sqlalchemy.ext.asyncio import AsyncSession

from druks.accounts.models import Account
from druks.chat.channels.base import Channel
from druks.chat.enums import ConversationSource
from druks.chat.models import Conversation, Message
from druks.chat.service import deliver
from druks.core.apis.slack import SLACK_AUTHORITY, SlackClient
from druks.core.services import Slack
from druks.redis import get_client
from druks.secrets.models import VaultSecret
from druks.settings import load_settings

from .constants import LINK_KEY, LINK_MESSAGE, LINK_TTL_SECONDS, REPLY_PIECE_CHARACTERS


class SlackChannel(Channel):
    name = ConversationSource.SLACK
    service = Slack

    @classmethod
    async def lookup_writer(
        cls, session: AsyncSession, card: VaultSecret, message: dict
    ) -> Account | None:
        """The account with a live Slack grant for the message's writer."""
        authority = SLACK_AUTHORITY.format(team_id=card.identity["team_id"])
        return await Account.lookup(session, authority, message["user"])

    @classmethod
    async def route_message(cls, session: AsyncSession, card: VaultSecret, message: dict) -> None:
        """Route a message for the bot: to the writer's own conversation, or to a private
        link that holds it until they connect their Slack account."""
        writer = await cls.lookup_writer(session, card, message)
        if writer:
            await cls.save_message(session, card, writer, message)
            return
        token = secrets.token_urlsafe(32)
        await get_client().set(
            LINK_KEY.format(token=token), json.dumps(message), ex=LINK_TTL_SECONDS
        )
        endpoint = load_settings().urls.endpoint.rstrip("/")
        url = f"{endpoint}/api/chat/services/slack/link/{token}"
        client = SlackClient(token=card.secrets["bot_token"])
        await client.post_markdown(message["user"], LINK_MESSAGE.format(url=url))

    @classmethod
    async def save_message(
        cls, session: AsyncSession, card: VaultSecret, writer: Account, message: dict
    ) -> Conversation:
        """Save the message in the writer's DM conversation and start its turn."""
        conversation = await Conversation.get_or_create_for_user(
            session,
            card,
            writer.id,
            source=ConversationSource.SLACK,
            user_id=message["user"],
            user_name="",
            user_phone="",
            thread_id="",
        )
        await conversation.create_message(
            session, message["text"], source_id=f"{message['channel']}:{message['ts']}"
        )
        await session.commit()
        await DBOS.start_workflow_async(deliver, conversation.id)
        return conversation

    @classmethod
    async def send_reply(
        cls, session: AsyncSession, conversation: Conversation, reply: Message
    ) -> None:
        """Post the reply in the person's DM, in pieces that fit a markdown block. The
        first piece's Slack id is the reply's source id."""
        client = SlackClient(token=conversation.connection.secrets["bot_token"])
        posted = [
            await client.post_markdown(
                conversation.user_id, reply.body[start : start + REPLY_PIECE_CHARACTERS]
            )
            for start in range(0, len(reply.body), REPLY_PIECE_CHARACTERS)
        ]
        reply.source_id = f"{posted[0]['channel']}:{posted[0]['ts']}"
        await session.commit()
