import aiohttp
from slack_sdk.errors import SlackApiError

from druks.core.apis.slack import SlackClient
from druks.notifications.buttons import encode_button
from druks.notifications.exceptions import DeliveryError, DisabledDestinationError
from druks.notifications.models import Destination

# What Slack's answer means when the bot cannot post to the channel, and the fix.
_CHANNEL_REASONS = {
    "not_in_channel": "the bot is not in channel {channel}. Invite the bot to the channel in Slack",
    "channel_not_found": (
        "Slack finds no channel {channel} for the bot. Check the channel ID. "
        "If the channel is private, invite the bot to it"
    ),
}


async def deliver(
    client: SlackClient,
    destination: Destination,
    body: str,
    *,
    actions: list[dict] | None = None,
    token: str | None = None,
    deep_link: str | None = None,
) -> None:
    """Each action becomes a button that carries ``token``, so a click answers this
    notification."""
    if not destination.is_enabled:
        raise DisabledDestinationError(destination.name)
    text = f"{body}\n{deep_link}" if deep_link else body
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
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
    # from None: these errors can keep the request, and its headers carry the bot token.
    try:
        await client.chat_postMessage(channel=destination.channel_id, text=text, blocks=blocks)
    except SlackApiError as error:
        reason = error.response.get("error") or f"HTTP {error.response.status_code}"
        if reason in _CHANNEL_REASONS:
            reason = _CHANNEL_REASONS[reason].format(channel=destination.channel_id)
        raise DeliveryError(destination.name, reason) from None
    except (aiohttp.ClientError, TimeoutError) as error:
        raise DeliveryError(destination.name, type(error).__name__) from None
