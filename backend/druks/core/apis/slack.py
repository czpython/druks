from collections import deque

from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from druks.redis import get_client

# The authority of a Slack grant: the workspace that a Slack user id belongs to.
SLACK_AUTHORITY = "https://slack.com/{team_id}"
# What the bot does in the workspace. The manifest asks for these.
SLACK_BOT_SCOPES = (
    "chat:write",
    "channels:history",
    "groups:history",
    "im:history",
    "mpim:history",
    "users:read",
)
USER_NAME_TTL_SECONDS = 24 * 60 * 60


class SlackClient(AsyncWebClient):
    """Slack's Web API under one token, the bot's or a person's, plus what Druks adds
    on top: the Markdown message it sends, a thread's tail, and the names it keeps."""

    async def post_markdown(
        self, channel: str, text: str, *, thread_ts: str = ""
    ) -> AsyncSlackResponse:
        """Post ``text`` as one markdown block, with the same text as the fallback, in a
        thread when ``thread_ts`` names one. A user id as the channel opens the bot's DM
        with that person."""
        return await self.chat_postMessage(
            channel=channel,
            text=text,
            blocks=[{"type": "markdown", "text": text}],
            thread_ts=thread_ts or None,
        )

    async def list_replies(self, channel: str, thread_ts: str, *, limit: int) -> list[dict]:
        """The thread's newest ``limit`` messages, oldest first. Slack pages a thread from
        its oldest message, so this walks to the end and keeps the tail."""
        messages: deque[dict] = deque(maxlen=limit)
        async for page in await self.conversations_replies(
            channel=channel, ts=thread_ts, limit=limit
        ):
            messages.extend(page["messages"])
        return list(messages)

    async def get_user_name(self, user_id: str) -> str:
        """A user's name, kept in Redis for a day."""
        key = f"slack:user:{user_id}:name"
        if name := await get_client().get(key):
            return name.decode()
        user = (await self.users_info(user=user_id))["user"]
        name = user["profile"]["display_name"] or user["real_name"]
        await get_client().set(key, name, ex=USER_NAME_TTL_SECONDS)
        return name
