from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.web.async_slack_response import AsyncSlackResponse

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


class SlackClient(AsyncWebClient):
    """Slack's Web API under one token, the bot's or a person's, plus the one message
    shape Druks sends: Markdown."""

    async def post_markdown(self, channel: str, text: str) -> AsyncSlackResponse:
        """Post ``text`` as one markdown block, with the same text as the fallback. A
        user id as the channel opens the bot's DM with that person."""
        return await self.chat_postMessage(
            channel=channel, text=text, blocks=[{"type": "markdown", "text": text}]
        )
