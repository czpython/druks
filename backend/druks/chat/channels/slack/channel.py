import json
import secrets
from datetime import timedelta

from dbos import DBOS
from sqlalchemy.ext.asyncio import AsyncSession

from druks.accounts.models import Account
from druks.chat.channels.base import Channel
from druks.chat.enums import ConversationSource
from druks.chat.exceptions import ChannelHasNoThreadsError
from druks.chat.models import Conversation, Message
from druks.chat.service import deliver
from druks.core.apis.slack import SLACK_AUTHORITY, SlackClient
from druks.core.services import Slack
from druks.files.datastructures import File
from druks.models import Base
from druks.redis import get_client
from druks.secrets.models import VaultSecret
from druks.settings import load_settings

from .constants import (
    FILE_KEY,
    FILE_TTL_SECONDS,
    JOINED_THREAD_SECONDS,
    LINK_KEY,
    LINK_MESSAGE,
    LINK_TTL_SECONDS,
    REPLY_PIECE_CHARACTERS,
    THREAD_FILES,
    THREAD_MESSAGES,
    UNLINKED_REPLY,
)


def get_thread_id(message: dict) -> str:
    """Where a message lives, as the room and the thread in one id. A direct message
    has none. A message at the top of a room starts a thread at itself."""
    if message["channel_type"] == "im":
        return ""
    return f"{message['channel']}:{message.get('thread_ts') or message['ts']}"


async def download_files(
    client: SlackClient, shares: list[dict], *, uploaded_by: str
) -> list[File]:
    """Each shared Slack file as a Druks file, in order. A copy made in the last day
    is found by Slack file id without a download, and the same file sent again is
    one file."""
    redis = get_client()
    keys = [FILE_KEY.format(file_id=shared["id"]) for shared in shares]
    held = [await redis.get(key) for key in keys]
    # Every download first: a failed one then leaves no file on disk.
    contents = [
        None if file_id else await client.download(shared["url_private_download"])
        for shared, file_id in zip(shares, held, strict=True)
    ]
    files = []
    for shared, key, file_id, content in zip(shares, keys, held, contents, strict=True):
        if file_id:
            files.append(File(id=file_id.decode()))
            continue
        file = await File.get_or_create(
            name=shared["name"],
            content_type=shared["mimetype"],
            content=content,
            app="chat",
            uploaded_by=uploaded_by,
        )
        await redis.set(key, file.id, ex=FILE_TTL_SECONDS)
        files.append(file)
    return files


class SlackChannel(Channel):
    name = ConversationSource.SLACK
    service = Slack

    @classmethod
    async def lookup_account(
        cls, session: AsyncSession, card: VaultSecret, message: dict
    ) -> Account | None:
        """The account with a live Slack grant for the Slack user who wrote the message."""
        authority = SLACK_AUTHORITY.format(team_id=card.identity["team_id"])
        return await Account.lookup(session, authority, message["user"])

    @classmethod
    async def route_message(cls, session: AsyncSession, card: VaultSecret, message: dict) -> None:
        """Route a message for the bot. A direct message or a tag reaches the linked
        account's own conversation, or waits under a private link until the person
        connects their Slack account. An untagged reply reaches the thread conversation
        the person used in the last day, unless they asked for a tag. Everything else is
        not for the bot."""
        thread_id = get_thread_id(message)
        is_addressed = not thread_id or f"<@{card.identity['bot_user_id']}>" in message["text"]
        if not is_addressed and "thread_ts" not in message:
            # Most of a room's traffic: a top-level message that names nobody.
            return
        linked_account = await cls.lookup_account(session, card, message)
        if is_addressed and linked_account:
            await cls.save_message(session, card, linked_account, message)
        elif is_addressed:
            await cls.send_link(card, message)
        elif linked_account:
            joined = await Conversation.get_for_user(
                session, card, user_id=message["user"], thread_id=thread_id
            )
            since = Base.utc_now() - timedelta(seconds=JOINED_THREAD_SECONDS)
            if joined and not joined.is_tag_required and joined.last_message_at > since:
                await cls.save_message(session, card, linked_account, message)

    @classmethod
    async def save_message(
        cls, session: AsyncSession, card: VaultSecret, account: Account, message: dict
    ) -> Conversation:
        """Save the message in the account's conversation for its place, and start its
        turn. Each file the message carries is a Druks file: the first on the message,
        each further one on a message of its own. A message with no text and no file
        gives the agent nothing to read."""
        conversation = await Conversation.get_or_create_for_user(
            session,
            card,
            account.id,
            source=ConversationSource.SLACK,
            user_id=message["user"],
            user_name="",
            user_phone="",
            thread_id=get_thread_id(message),
            is_tag_required=account.is_tag_required,
        )
        client = SlackClient(token=card.secrets["bot_token"])
        shares = message.get("files", [])
        files = await download_files(client, shares, uploaded_by=account.id)
        # The agent would take the bot's own mention for the person it answers.
        text = message["text"].replace(f"<@{card.identity['bot_user_id']}>", "").strip()
        source_id = f"{message['channel']}:{message['ts']}"
        if files:
            await conversation.create_message(session, text, source_id=source_id, file=files[0])
            for shared, file in zip(shares[1:], files[1:], strict=True):
                await conversation.create_message(
                    session, "", source_id=f"{source_id}:{shared['id']}", file=file
                )
        elif text:
            await conversation.create_message(session, text, source_id=source_id)
        else:
            return conversation
        await session.commit()
        await DBOS.start_workflow_async(deliver, conversation.id)
        return conversation

    @classmethod
    async def send_link(cls, card: VaultSecret, message: dict) -> None:
        """Hold the message under a private link, and send the person the link in their
        DM. In a room, also tell them in the thread where the link is."""
        token = secrets.token_urlsafe(32)
        await get_client().set(
            LINK_KEY.format(token=token), json.dumps(message), ex=LINK_TTL_SECONDS
        )
        endpoint = load_settings().urls.endpoint.rstrip("/")
        client = SlackClient(token=card.secrets["bot_token"])
        # Slack drops a message that only one person sees unless they are active in
        # that client, so the link goes to their DM, which keeps it.
        await client.post_markdown(
            message["user"],
            LINK_MESSAGE.format(url=f"{endpoint}/api/chat/services/slack/link/{token}"),
        )
        if thread_id := get_thread_id(message):
            room, _, thread_ts = thread_id.partition(":")
            await client.post_markdown(
                room, UNLINKED_REPLY.format(user=message["user"]), thread_ts=thread_ts
            )

    @classmethod
    async def send_reply(
        cls, session: AsyncSession, conversation: Conversation, reply: Message
    ) -> None:
        """Post the reply where the conversation lives, in pieces that fit a markdown
        block. The first piece's Slack id is the reply's source id."""
        client = SlackClient(token=conversation.connection.secrets["bot_token"])
        room, _, thread_ts = conversation.thread_id.partition(":")
        posted = [
            await client.post_markdown(
                room or conversation.user_id,
                reply.body[start : start + REPLY_PIECE_CHARACTERS],
                thread_ts=thread_ts,
            )
            for start in range(0, len(reply.body), REPLY_PIECE_CHARACTERS)
        ]
        reply.source_id = f"{posted[0]['channel']}:{posted[0]['ts']}"
        await session.commit()

    @classmethod
    async def read_thread(cls, session: AsyncSession, conversation: Conversation) -> list[dict]:
        """The thread's newest messages, oldest first, with who wrote each one and the
        files on it. The thread's newest files are Druks files the agent fetches by
        url; an older one is named without a url. Every agent in the thread posts as
        the one bot, so a reply is this conversation's by its recorded Slack id. A
        webhook's post has no author and is left out."""
        room, _, thread_ts = conversation.thread_id.partition(":")
        if not room:
            raise ChannelHasNoThreadsError("A direct message has no thread to read.")
        client = SlackClient(token=conversation.connection.secrets["bot_token"])
        messages = [
            message
            for message in await client.list_replies(room, thread_ts, limit=THREAD_MESSAGES)
            if "user" in message
        ]
        own_replies = await conversation.list_reply_source_ids(session)
        shares = [shared for message in messages for shared in message.get("files", [])]
        shares = shares[-THREAD_FILES:]
        files = await download_files(client, shares, uploaded_by=conversation.account_id)
        base = load_settings().urls.webhook_base
        urls = {
            shared["id"]: f"{base}{file.url}" for shared, file in zip(shares, files, strict=True)
        }
        return [
            {
                "ts": message["ts"],
                "user_id": message["user"],
                "user_name": await client.get_user_name(message["user"]),
                "text": message["text"],
                "files": [
                    {
                        "name": shared["name"],
                        "content_type": shared["mimetype"],
                        "url": urls.get(shared["id"], ""),
                    }
                    for shared in message.get("files", [])
                ],
                "is_from_you": f"{room}:{message['ts']}" in own_replies,
                "is_from_user": message["user"] == conversation.user_id,
            }
            for message in messages
        ]
