import json
import logging
import secrets
from datetime import timedelta

from dbos import DBOS
from slack_sdk.errors import SlackApiError
from sqlalchemy.ext.asyncio import AsyncSession

from druks.accounts.models import Account
from druks.agents.constants import PRIVATE_AGENT_MESSAGE
from druks.agents.models import Agent
from druks.apps.loader import iter_apps
from druks.chat.channels.base import Channel
from druks.chat.enums import ConversationSource
from druks.chat.exceptions import ChannelHasNoThreadsError, FileNotInThreadError
from druks.chat.models import Automation, Conversation, Message
from druks.chat.service import deliver
from druks.core.apis.slack import SlackClient
from druks.core.services import Slack
from druks.files.datastructures import File
from druks.models import Base
from druks.notifications.constants import CLOSED_MESSAGE, MARKDOWN_TEXT_LIMIT
from druks.notifications.delivery import deliver as deliver_notification
from druks.notifications.models import Notification
from druks.prompts import render_prompt
from druks.redis import get_client
from druks.secrets.models import VaultSecret
from druks.settings import get_settings

from .constants import (
    JOINED_THREAD_SECONDS,
    LINK_KEY,
    LINK_MESSAGE,
    LINK_TTL_SECONDS,
    THREAD_MESSAGES,
    UNACCEPTED_AUTOMATION_REPLY,
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
    has_buttons = True
    reaction_ack = "eyes"

    @classmethod
    async def lookup_account(
        cls, session: AsyncSession, row: VaultSecret, message: dict
    ) -> Account | None:
        """The account with a live Slack grant for the Slack user who wrote the message."""
        authority = Slack.Authentication.get_authority(row.identity["team_id"])
        owner_id = await VaultSecret.get_owner_id(session, authority, message["user"])
        return await session.get(Account, owner_id) if owner_id else None

    @classmethod
    async def route_message(cls, session: AsyncSession, row: VaultSecret, message: dict) -> None:
        """Route a message for the bot. A direct message or a tag reaches the linked
        account's own conversation, or waits under a private link until the person
        connects their Slack account. A tag followed by an agent's name reaches the
        person's conversation with that agent. A tag that names none reaches the
        person's latest conversation in the thread, or else the channel's agent. An
        untagged reply reaches the thread conversation the person used in the last day,
        unless they asked for a tag. The tag of an automation, which is another app's bot
        or a workflow, reaches the default account once an admin accepted the automation,
        and its first tag waits for that. Everything else is not for the bot."""
        if bot_id := message.get("bot_id"):
            automation = await Automation.get_or_none(
                session, connection_id=row.id, source_id=bot_id
            )
            if automation and automation.is_accepted:
                account = await Account.get_default(session)
                await cls.save_message(session, row, account, message, agent_id=None)
            elif not automation:
                await cls.hold_message(session, row, message)
            return
        thread_id = get_thread_id(message)
        mention = f"<@{row.identity['bot_user_id']}>"
        is_tagged = mention in message["text"]
        is_addressed = not thread_id or is_tagged
        if not is_addressed and "thread_ts" not in message:
            # Most of a room's traffic: a top-level message that names nobody.
            return
        linked_account = await cls.lookup_account(session, row, message)
        if is_addressed and linked_account:
            agent = None
            if is_tagged:
                # "@Druks, Security engineer" and "@Druks: Security engineer" name it too.
                text = message["text"].partition(mention)[2].lstrip(",: \n")
                agent = await Agent.lookup(session, text)
                if not agent and thread_id:
                    latest = await Conversation.get_for_user(
                        session, row, user_id=message["user"], thread_id=thread_id
                    )
                    if latest:
                        agent = latest.agent
                    else:
                        agent = await Agent.get_for_channel(session, message["channel"])
            if agent and not linked_account.can_talk_to(agent):
                room, _, thread_ts = thread_id.partition(":")
                client = SlackClient(token=await Slack.get_bot_token())
                await client.post_markdown(
                    room or message["user"], PRIVATE_AGENT_MESSAGE, thread_ts=thread_ts
                )
                return
            await cls.save_message(
                session, row, linked_account, message, agent_id=agent.id if agent else None
            )
        elif is_addressed:
            await cls.send_link(message)
        elif linked_account:
            joined = await Conversation.get_for_user(
                session, row, user_id=message["user"], thread_id=thread_id
            )
            since = Base.utc_now() - timedelta(seconds=JOINED_THREAD_SECONDS)
            if joined and not joined.is_tag_required and joined.last_message_at > since:
                await cls.save_message(
                    session, row, linked_account, message, agent_id=joined.agent_id
                )

    @classmethod
    async def save_message(
        cls,
        session: AsyncSession,
        row: VaultSecret,
        account: Account,
        message: dict,
        *,
        agent_id: str | None,
    ) -> Conversation:
        """Save the message in the account's conversation with the agent, or with the
        Bot, for its place, and start its turn. Each file the message carries is a Druks
        file: the first on the message, each further one on a message of its own. A
        message with no text and no file gives the agent nothing to read."""
        conversation = await Conversation.get_or_create_for_user(
            session,
            row,
            account.id,
            source=ConversationSource.SLACK,
            # A workflow's post has no user, so its bot ID stands for the person.
            user_id=message.get("user") or message["bot_id"],
            user_name="",
            user_phone="",
            thread_id=get_thread_id(message),
            is_tag_required=account.is_tag_required,
            agent_id=agent_id,
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
    async def hold_message(cls, session: AsyncSession, row: VaultSecret, message: dict) -> None:
        """Keep the first tag of an automation until an admin accepts the automation,
        and tell the thread where an admin does that."""
        client = SlackClient(token=await Slack.get_bot_token())
        bot = (await client.bots_info(bot=message["bot_id"]))["bot"]
        session.add(
            Automation(
                connection_id=row.id, source_id=bot["id"], name=bot["name"], first_message=message
            )
        )
        # Saved before the post: a second first tag of one automation fails here and
        # posts nothing.
        await session.flush()
        endpoint = get_settings().urls.endpoint.rstrip("/")
        room, _, thread_ts = get_thread_id(message).partition(":")
        await client.post_markdown(
            room,
            UNACCEPTED_AUTOMATION_REPLY.format(url=f"{endpoint}/apps/chat/settings/channels"),
            thread_ts=thread_ts,
        )

    @classmethod
    async def send_reply(
        cls, session: AsyncSession, conversation: Conversation, reply: Message
    ) -> None:
        """Post the reply where the conversation lives, in pieces that fit a markdown
        block. The first piece's Slack id is the reply's source id. An agent's reply
        shows the agent's name and picture."""
        client = SlackClient(token=await Slack.get_bot_token())
        room, _, thread_ts = conversation.thread_id.partition(":")
        # Slack takes a custom name only with the scope. A workspace that has not
        # approved the app again gets the reply under the app's own name.
        grant = await Slack.get_grant()
        agent = conversation.agent if "chat:write.customize" in grant.scopes else None
        posted = [
            await client.post_markdown(
                room or conversation.user_id,
                reply.answer[start : start + MARKDOWN_TEXT_LIMIT],
                thread_ts=thread_ts,
                username=agent.name if agent else None,
                icon_url=agent.picture_url if agent else None,
            )
            for start in range(0, len(reply.answer), MARKDOWN_TEXT_LIMIT)
        ]
        reply.source_id = f"{posted[0]['channel']}:{posted[0]['ts']}"
        await session.commit()

    @classmethod
    async def post_buttons(
        cls, session: AsyncSession, conversation: Conversation, notification: Notification
    ) -> str:
        """Post the notification in the conversation's thread, or its DM. A notification
        that an answer closed before the post says that its question is closed, in place
        of its buttons."""
        client = SlackClient(token=await Slack.get_bot_token())
        room, _, thread_ts = conversation.thread_id.partition(":")
        actions, outcome = notification.actions, ""
        if notification.is_acknowledged:
            actions, outcome = None, CLOSED_MESSAGE
        posted = await deliver_notification(
            client,
            room or conversation.user_id,
            notification.body,
            thread_ts=thread_ts,
            actions=actions,
            token=notification.correlation_token,
            deep_link=notification.deep_link,
            outcome=outcome,
            question_offset=notification.question_offset,
        )
        return f"{posted['channel']}:{posted['ts']}"

    @classmethod
    async def remove_buttons(
        cls, session: AsyncSession, conversation: Conversation, notification: Notification
    ) -> None:
        """Edit the notification's message to say that its question is closed."""
        client = SlackClient(token=await Slack.get_bot_token())
        room, message_ts = notification.message_id.split(":")
        await deliver_notification(
            client,
            room,
            notification.body,
            message_ts=message_ts,
            deep_link=notification.deep_link,
            outcome=CLOSED_MESSAGE,
            question_offset=notification.question_offset,
        )

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
