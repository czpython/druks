import re

import aiohttp
from slack_sdk.errors import SlackApiError

from druks.core.apis.slack import SlackClient
from druks.notifications.buttons import encode_button
from druks.notifications.constants import MARKDOWN_TEXT_LIMIT, TRUNCATION_MARKER
from druks.notifications.exceptions import ChannelUnavailableError, DeliveryError

# What Slack's answer means when the bot cannot post to the channel, and the fix.
_CHANNEL_REASONS = {
    "not_in_channel": "the bot is not in channel {channel}. Invite the bot to the channel in Slack",
    "channel_not_found": (
        "Slack finds no channel {channel} for the bot. Check the channel ID. "
        "If the channel is private, invite the bot to it"
    ),
    "is_archived": "channel {channel} is archived. Choose another channel",
}

# A line that opens or closes a code fence: its indent, three or more backticks or
# tildes, then the rest.
_FENCE = re.compile(r"(\s*)(`{3,}|~{3,})(.*)")


def cut_markdown(text: str, limit: int) -> str:
    """``text`` when it fits in ``limit`` characters. Otherwise, the most whole lines of
    ``text`` that fit with the marker after them. A code fence that those lines leave open
    closes before the marker, so the marker and what follows it stay outside the fence.
    When not even the first line fits, its start takes the place of the lines."""
    if len(text) <= limit:
        return text
    room = limit - len(TRUNCATION_MARKER)
    lines: list[str] = []
    # A newline goes before each line but the first.
    size = -1
    open_fence = ""
    for line in text.split("\n"):
        fence_after = open_fence
        if match := _FENCE.match(line):
            indent, fence, rest = match.groups()
            if open_fence:
                if fence.startswith(open_fence.lstrip()) and not rest.strip():
                    fence_after = ""
            # Backticks with another backtick after them on the line are inline code.
            elif fence[0] == "~" or "`" not in rest:
                # The closer takes the opener's indent, so a fence in a list item closes in it.
                fence_after = indent + fence
        closing = f"\n{fence_after}" if fence_after else ""
        if size + 1 + len(line) + len(closing) > room:
            break
        lines.append(line)
        size += 1 + len(line)
        open_fence = fence_after
    if lines:
        kept = "\n".join(lines).rstrip("\n")
        return kept + (f"\n{open_fence}" if open_fence else "") + TRUNCATION_MARKER
    # The loop stopped at the first line, so ``closing`` closes the fence that it opens.
    return text[: room - len(closing)] + closing + TRUNCATION_MARKER


async def deliver(
    client: SlackClient,
    channel: str,
    body: str,
    *,
    thread_ts: str = "",
    message_ts: str = "",
    user: str = "",
    actions: list[dict] | None = None,
    token: str | None = None,
    deep_link: str | None = None,
    outcome: str = "",
) -> dict:
    """Post ``body`` as markdown, with ``deep_link`` on the line after it. Each action
    becomes a button that carries ``token``, so a click answers this notification. With
    ``message_ts``, Druks edits that posted message, and ``outcome`` takes the place of its
    buttons. With ``user``, only that person sees the message. Returns Slack's answer,
    which names the message's channel and ts."""
    link = f"\n{deep_link}" if deep_link else ""
    # Slack refuses markdown longer than this, so a long body loses its end and
    # keeps the link, which opens the full text.
    text = cut_markdown(body, MARKDOWN_TEXT_LIMIT - len(link)) + link
    blocks = [{"type": "markdown", "text": text}]
    if actions:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": action["label"]},
                        "action_id": encode_button(token, action["id"]),
                    }
                    for action in actions
                ],
            }
        )
    if outcome:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": outcome}})
    # from None: these errors can keep the request, and its headers carry the bot token.
    try:
        if message_ts:
            response = await client.chat_update(
                channel=channel, ts=message_ts, text=text, blocks=blocks
            )
        elif user:
            response = await client.chat_postEphemeral(
                channel=channel, user=user, text=text, blocks=blocks, thread_ts=thread_ts or None
            )
        else:
            response = await client.chat_postMessage(
                channel=channel, text=text, blocks=blocks, thread_ts=thread_ts or None
            )
    except SlackApiError as error:
        reason = error.response.get("error") or f"HTTP {error.response.status_code}"
        if reason in _CHANNEL_REASONS:
            raise ChannelUnavailableError(
                _CHANNEL_REASONS[reason].format(channel=channel)
            ) from None
        raise DeliveryError(reason) from None
    except (aiohttp.ClientError, TimeoutError) as error:
        raise DeliveryError(type(error).__name__) from None
    return response.data
