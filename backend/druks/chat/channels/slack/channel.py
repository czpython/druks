import json
import logging
import secrets
from datetime import timedelta

from dbos import DBOS
from slack_sdk.errors import SlackApiError
from sqlalchemy.ext.asyncio import AsyncSession

from druks.accounts.models import Account
from druks.apps.loader import iter_apps
from druks.chat.channels.base import Channel
from druks.chat.enums import ConversationSource
from druks.chat.exceptions import ChannelHasNoThreadsError, FileNotInThreadError
from druks.chat.models import Conversation, Message
from druks.chat.service import deliver
from druks.core.apis.slack import SLACK_AUTHORITY, SlackClient
from druks.core.services import Slack
from druks.files.datastructures import File
from druks.models import Base
from druks.prompts import render_prompt
from druks.redis import get_client
from druks.secrets.models import VaultSecret
from druks.settings import get_settings

from .constants import (
    JOINED_THREAD_SECONDS,
    LINK_KEY,
    LINK_MESSAGE,
    LINK_TTL_SECONDS,
    REPLY_PIECE_CHARACTERS,
    THREAD_MESSAGES,
    UNLINKED_REPLY,
    WELCOME_KEY,
    WELCOME_TEMPLATE,
    WELCOME_TTL_SECONDS,
)

logger = logging.getLogger(__name__)


def get_thread_id(message: dict) -> str:
    """Where a message lives, as the room and the thread in one id. A direct message
    has none. A message at the top of a room starts a thread at itself."""
    if message["channel_type"] == "im":
        return ""
    return f"{message['channel']}:{message.get('thread_ts') or message['ts']}"


class SlackChannel(Channel):
    name = ConversationSource.SLACK
    service = Slack
    has_readable_threads = True
    reaction_ack = "eyes"

    @classmethod
    async def lookup_account(
        cls, session: AsyncSession, row: VaultSecret, message: dict
    ) -> Account | None:
        """The account with a live Slack grant for the Slack user who wrote the message."""
        authority = SLACK_AUTHORITY.format(team_id=row.identity["team_id"])
        owner_id = await VaultSecret.get_owner_id(session, authority, message["user"])
        return await session.get(Account, owner_id) if owner_id else None

    @classmethod
    async def route_message(cls, session: AsyncSession, row: VaultSecret, message: dict) -> None:
        """Route a message for the bot. A direct message or a tag reaches the linked
        account's own conversation, or waits under a private link until the person
        connects their Slack account. An untagged reply reaches the thread conversation
        the person used in the last day, unless they asked for a tag. Everything else is
        not for the bot."""
        thread_id = get_thread_id(message)
        is_addressed = not thread_id or f"<@{row.identity['bot_user_id']}>" in message["text"]
        if not is_addressed and "thread_ts" not in message:
            # Most of a room's traffic: a top-level message that names nobody.
            return
        linked_account = await cls.lookup_account(session, row, message)
        if is_addressed and linked_account:
            await cls.save_message(session, row, linked_account, message)
        elif is_addressed:
            await cls.send_link(message)
        elif linked_account:
            joined = await Conversation.get_for_user(
                session, row, user_id=message["user"], thread_id=thread_id
            )
            since = Base.utc_now() - timedelta(seconds=JOINED_THREAD_SECONDS)
            if joined and not joined.is_tag_required and joined.last_message_at > since:
                await cls.save_message(session, row, linked_account, message)

    @classmethod
    async def save_message(
        cls, session: AsyncSession, row: VaultSecret, account: Account, message: dict
    ) -> Conversation:
        """Save the message in the account's conversation for its place, and start its
        turn. Each file the message carries is a Druks file: the first on the message,
        each further one on a message of its own. A message with no text and no file
        gives the agent nothing to read."""
        conversation = await Conversation.get_or_create_for_user(
            session,
            row,
            account.id,
            source=ConversationSource.SLACK,
            user_id=message["user"],
            user_name="",
            user_phone="",
            thread_id=get_thread_id(message),
            is_tag_required=account.is_tag_required,
        )
        client = SlackClient(token=await Slack.get_bot_token())
        shares = message.get("files", [])
        # Every download first: a failed one then leaves no file on disk.
        contents = [await client.download(shared["url_private_download"]) for shared in shares]
        files = [
            await File.create(
                name=shared["name"],
                content_type=shared["mimetype"],
                content=content,
                app="chat",
                uploaded_by=account.id,
            )
            for shared, content in zip(shares, contents, strict=True)
        ]
        # The agent would take the bot's own mention for the person it answers.
        text = message["text"].replace(f"<@{row.identity['bot_user_id']}>", "").strip()
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
    async def get_welcome(cls) -> str:
        """What the bot says when it meets a room or a person, for this installation's
        apps and address."""
        return await render_prompt(
            WELCOME_TEMPLATE,
            apps=[app for app in iter_apps() if app.description and not app.builtin],
            endpoint=get_settings().urls.endpoint.rstrip("/"),
        )

    @classmethod
    async def send_link(cls, message: dict) -> None:
        """Hold the message under a private link, and send the person the link in their
        DM, after the welcome if they have not had it. In a room, also tell them in the
        thread where the link is."""
        token = secrets.token_urlsafe(32)
        await get_client().set(
            LINK_KEY.format(token=token), json.dumps(message), ex=LINK_TTL_SECONDS
        )
        endpoint = get_settings().urls.endpoint.rstrip("/")
        client = SlackClient(token=await Slack.get_bot_token())
        is_first_contact = await get_client().set(
            WELCOME_KEY.format(user=message["user"]), "1", nx=True, ex=WELCOME_TTL_SECONDS
        )
        if is_first_contact:
            await client.post_markdown(message["user"], await cls.get_welcome())
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
        client = SlackClient(token=await Slack.get_bot_token())
        room, _, thread_ts = conversation.thread_id.partition(":")
        posted = [
            await client.post_markdown(
                room or conversation.user_id,
                reply.answer[start : start + REPLY_PIECE_CHARACTERS],
                thread_ts=thread_ts,
            )
            for start in range(0, len(reply.answer), REPLY_PIECE_CHARACTERS)
        ]
        reply.source_id = f"{posted[0]['channel']}:{posted[0]['ts']}"
        await session.commit()

    @classmethod
    async def acknowledge(
        cls, session: AsyncSession, conversation: Conversation, message: Message
    ) -> None:
        """React to the person's message."""
        client = SlackClient(token=await Slack.get_bot_token())
        room, ts = message.source_id.split(":")[:2]
        try:
            await client.reactions_add(channel=room, timestamp=ts, name=cls.reaction_ack)
        except SlackApiError as error:
            if error.response["error"] == "already_reacted":
                logger.info("Slack message %s already has the reaction", message.source_id)
                return
            raise

    @classmethod
    def get_block_text(cls, block: dict) -> str:
        """A link shows its URL so the agent can follow it."""
        match block["type"]:
            case "plain_text" | "mrkdwn" | "text":
                return block["text"]
            case "link":
                if block.get("text"):
                    return f"{block['text']} ({block['url']})"
                return block["url"]
            case "user":
                return f"<@{block['user_id']}>"
            case "channel":
                return f"<#{block['channel_id']}>"
            case "emoji":
                return f":{block['name']}:"
            case "header":
                return cls.get_block_text(block["text"])
            case "section":
                pieces = [block.get("text"), *block.get("fields", [])]
                return "\n".join(cls.get_block_text(piece) for piece in pieces if piece)
            case "context":
                return " ".join(cls.get_block_text(element) for element in block["elements"])
            case "rich_text" | "rich_text_list":
                return "\n".join(cls.get_block_text(element) for element in block["elements"])
            case "rich_text_section" | "rich_text_preformatted" | "rich_text_quote":
                return "".join(cls.get_block_text(element) for element in block["elements"])
        return ""

    @classmethod
    def get_message_text(cls, message: dict, *, bot_user_id: str) -> str:
        """A person's message repeats its text as a rich text block, so rich text counts
        only when the text is empty. Druks puts its whole message in the text, and its
        blocks repeat it."""
        text = message["text"]
        if message["user"] == bot_user_id:
            return text
        pieces = [text]
        for attachment in message.get("attachments", []):
            title = attachment.get("title", "")
            if attachment.get("title_link"):
                title = f"{title} ({attachment['title_link']})"
            pieces += [attachment.get("pretext", ""), title, attachment.get("text", "")]
            pieces += [
                ": ".join(part for part in (field.get("title"), field.get("value")) if part)
                for field in attachment.get("fields", [])
            ]
            pieces += [cls.get_block_text(block) for block in attachment.get("blocks", [])]
            pieces.append(attachment.get("footer", ""))
        pieces += [
            cls.get_block_text(block)
            for block in message.get("blocks", [])
            if not text or block["type"] != "rich_text"
        ]
        return "\n".join(piece for piece in pieces if piece)

    @classmethod
    async def read_thread(cls, session: AsyncSession, conversation: Conversation) -> list[dict]:
        """The thread's newest messages, oldest first, with who wrote each one and the
        files on it. Every agent in the thread posts as the one bot, so a reply is this
        conversation's by its recorded Slack id. A webhook's post has no author and is
        left out."""
        room, _, thread_ts = conversation.thread_id.partition(":")
        if not room:
            raise ChannelHasNoThreadsError("A direct message has no thread to read.")
        client = SlackClient(token=await Slack.get_bot_token())
        messages = await client.list_replies(room, thread_ts, limit=THREAD_MESSAGES)
        own_replies = await conversation.list_reply_source_ids(session)
        bot_user_id = (await Slack.get()).identity["bot_user_id"]
        return [
            {
                "ts": message["ts"],
                "user_id": message["user"],
                "user_name": await client.get_user_name(message["user"]),
                "text": cls.get_message_text(message, bot_user_id=bot_user_id),
                "files": [
                    {
                        "id": shared["id"],
                        "name": shared["name"],
                        "content_type": shared["mimetype"],
                    }
                    for shared in message.get("files", [])
                ],
                "is_from_you": f"{room}:{message['ts']}" in own_replies,
                "is_from_user": message["user"] == conversation.user_id,
            }
            for message in messages
            if "user" in message
        ]

    @classmethod
    async def download_file(
        cls, session: AsyncSession, conversation: Conversation, file_id: str
    ) -> tuple[bytes, str, str]:
        """A file shared in the room where the conversation's thread lives, by its
        Slack id, as its bytes, name, and content type."""
        room, _, _ = conversation.thread_id.partition(":")
        if not room:
            raise ChannelHasNoThreadsError("A direct message has no thread to read.")
        client = SlackClient(token=await Slack.get_bot_token())
        shared = (await client.files_info(file=file_id))["file"]
        if room not in shared["channels"] + shared["groups"]:
            raise FileNotInThreadError(f"File {file_id} is not shared where this thread lives.")
        content = await client.download(shared["url_private_download"])
        return content, shared["name"], shared["mimetype"]
