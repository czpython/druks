import re

from dbos import DBOS
from sqlalchemy.ext.asyncio import AsyncSession

from druks.accounts.models import Account
from druks.chat.channels.base import Channel
from druks.chat.enums import ConversationSource
from druks.chat.models import Conversation, Message
from druks.chat.service import deliver
from druks.core.apis.github import GITHUB_AUTHORITY
from druks.core.services import Github
from druks.secrets.models import VaultSecret

from .constants import CONNECT_MESSAGE, THREAD_MESSAGES


def get_source_id(comment_id: int, review_thread_id: int | None) -> str:
    """A comment's id at GitHub, with the inline thread GitHub takes replies under."""
    if review_thread_id:
        return f"review_comment:{review_thread_id}:{comment_id}"
    return f"issue_comment:{comment_id}"


def is_tagged(body: str, handle: str) -> bool:
    """Whether the comment tags the App outside a quoted line. Only the full handle counts."""
    lines = [line for line in body.splitlines() if not line.startswith(">")]
    tag = rf"(?<!\w)@{re.escape(handle)}(?![\w-])"
    return bool(re.search(tag, "\n".join(lines), re.IGNORECASE))


class GitHubChannel(Channel):
    name = ConversationSource.GITHUB
    service = Github

    @classmethod
    async def lookup_account(cls, session: AsyncSession, payload: dict) -> Account | None:
        """The account with a live GitHub sign-in for the person who wrote the comment."""
        return await Account.lookup(session, GITHUB_AUTHORITY, str(payload["author_id"]))

    @classmethod
    async def route_message(
        cls, session: AsyncSession, card: VaultSecret, thread_id: str, payload: dict
    ) -> None:
        """A tag reaches the linked account's conversation for the issue. An unlinked
        writer is told where to connect GitHub."""
        is_addressed = is_tagged(payload["body"], card.identity["slug"])
        source_id = get_source_id(payload["comment_id"], payload["review_thread_id"])
        if is_addressed and not await Message.get_for_source_id(session, source_id):
            linked_account = await cls.lookup_account(session, payload)
            if linked_account:
                await cls.save_message(session, card, linked_account, thread_id, payload)
            else:
                text = CONNECT_MESSAGE.format(author=payload["author"])
                await cls.post(thread_id, text, source_id=source_id)

    @classmethod
    async def save_message(
        cls,
        session: AsyncSession,
        card: VaultSecret,
        account: Account,
        thread_id: str,
        payload: dict,
    ) -> Conversation:
        """Save the comment in the account's conversation for the issue, and start its turn."""
        conversation = await Conversation.get_or_create_for_user(
            session,
            card,
            account.id,
            source=ConversationSource.GITHUB,
            user_id=str(payload["author_id"]),
            user_name=payload["author"],
            user_phone="",
            thread_id=thread_id,
        )
        conversation.title = thread_id
        source_id = get_source_id(payload["comment_id"], payload["review_thread_id"])
        await conversation.create_message(session, payload["body"], source_id=source_id)
        await session.commit()
        await DBOS.start_workflow_async(deliver, conversation.id)
        return conversation

    @classmethod
    async def post(cls, thread_id: str, body: str, *, source_id: str) -> str:
        """Post ``body`` where the comment ``source_id`` sits: in its inline thread, or
        at the top of the issue. Returns the posted comment's source id."""
        client = await Github.get_client()
        repo, _, number = thread_id.partition("#")
        kind, *place = source_id.split(":")
        if kind == "review_comment":
            thread = int(place[0])
            posted = await client.reply_to_review_comment(repo, int(number), thread, body)
            return get_source_id(posted["id"], thread)
        posted = await client.create_comment(repo, int(number), body)
        return get_source_id(posted["id"], None)

    @classmethod
    async def send_reply(
        cls, session: AsyncSession, conversation: Conversation, reply: Message
    ) -> None:
        """Post the reply where the tag it answers was, or the last tag before a message
        Druks wrote."""
        asked = await session.get(Message, reply.reply_to)
        source_id = await conversation.get_answered_source_id(session, asked)
        reply.source_id = await cls.post(conversation.thread_id, reply.body, source_id=source_id)
        await session.commit()

    @classmethod
    async def get_prompt_context(cls, session: AsyncSession, conversation: Conversation) -> dict:
        """Whether the repository is private, so the agent knows who can read its reply,
        and the login of the person it answers."""
        repo = conversation.thread_id.partition("#")[0]
        repository = await (await Github.get_client()).get_repository(repo)
        return {"is_private": repository["private"], "user_name": conversation.user_name}

    @classmethod
    async def read_thread(cls, session: AsyncSession, conversation: Conversation) -> list[dict]:
        """The issue or pull request, then its newest comments, oldest first. A reply is
        this conversation's by its recorded GitHub id."""
        client = await Github.get_client()
        repo, _, number = conversation.thread_id.partition("#")
        number = int(number)
        own_replies = await conversation.list_reply_source_ids(session)

        def read(post: dict, source_id: str, *, path: str = "", line: int | None = None) -> dict:
            author = str(post["user"]["id"])
            return {
                "ts": post["created_at"].isoformat(),
                "user_id": author,
                "user_name": post["user"]["login"],
                "text": post["body"],
                "path": path,
                "line": line,
                "is_from_you": source_id in own_replies,
                "is_from_user": author == conversation.user_id,
            }

        issue = await client.get_issue(repo, number)
        issue["body"] = f"{issue['title']}\n\n{issue['body'] or ''}"
        entries = [
            read(comment, get_source_id(comment["id"], None))
            for comment in await client.list_comments(repo, number)
        ]
        if issue["pull_request"]:
            for comment in await client.list_review_comments(repo, number):
                thread = comment["in_reply_to_id"] or comment["id"]
                source_id = get_source_id(comment["id"], thread)
                entries.append(read(comment, source_id, path=comment["path"], line=comment["line"]))
        entries.sort(key=lambda entry: entry["ts"])
        return [read(issue, f"issue:{number}"), *entries[-THREAD_MESSAGES:]]
