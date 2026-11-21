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

SUBMIT_ACTION = {"id": "submit", "label": "Submit"}

# A number field takes text: Slack's number input works only in a modal.
FIELD_ELEMENTS = {
    "text": "plain_text_input",
    "text_area": "plain_text_input",
    "number": "plain_text_input",
    "select": "static_select",
    "multi_select": "multi_static_select",
    "radio": "radio_buttons",
    "checkbox": "checkboxes",
}

SECTION_TEXT_LIMIT = 3000


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


def get_input(field: dict) -> dict:
    """The input block that shows a form's ``field``: its label, its help text as the hint,
    whether it is optional, and an element that holds its default. The block's id is the
    field's name, so the answer in the element comes back under that name."""
    element: dict = {"type": FIELD_ELEMENTS[field["field"]]}
    options = [
        {"text": {"type": "plain_text", "text": option["label"]}, "value": option["value"]}
        for option in field.get("options", [])
    ]
    match field:
        case {"field": "text" | "text_area", "value": value, "placeholder": placeholder}:
            element["multiline"] = field["field"] == "text_area"
            if value:
                element["initial_value"] = value
            if placeholder:
                element["placeholder"] = {"type": "plain_text", "text": placeholder}
        case {"field": "number", "value": value} if value is not None:  # ast-grep-ignore: rule-2
            element["initial_value"] = str(value).removesuffix(".0")
        case {"field": "multi_select", "value": value}:
            element["options"] = options
            if chosen := [option for option in options if option["value"] in value]:
                element["initial_options"] = chosen
        case {"field": "select" | "radio", "value": value}:
            element["options"] = options
            if chosen := [option for option in options if option["value"] == value]:
                element["initial_option"] = chosen[0]
        case {"field": "checkbox", "value": value}:
            box = {"text": {"type": "plain_text", "text": "Yes"}, "value": "yes"}
            element["options"] = [box]
            if value:
                element["initial_options"] = [box]
    block = {
        "type": "input",
        "block_id": field["name"],
        "label": {"type": "plain_text", "text": field["label"]},
        "optional": not field["is_required"],
        "element": element,
    }
    if field["help_text"]:
        block["hint"] = {"type": "plain_text", "text": field["help_text"]}
    return block


def get_blocks(
    text: str,
    *,
    actions: list[dict] | None = None,
    fields: list[dict] | None = None,
    token: str | None = None,
    outcome: str = "",
) -> list[dict]:
    """The blocks of a notification's message: ``text`` as markdown, an input for each of
    ``fields``, a button that carries ``token`` for each action, then ``outcome``. A form's
    one button is its Submit button."""
    blocks = [{"type": "markdown", "text": text}]
    if fields:
        blocks += [get_input(field) for field in fields]
        actions = [SUBMIT_ACTION]
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
        section = {"type": "mrkdwn", "text": cut_markdown(outcome, SECTION_TEXT_LIMIT)}
        blocks.append({"type": "section", "text": section})
    return blocks


async def deliver(
    client: SlackClient,
    channel: str,
    body: str,
    *,
    thread_ts: str = "",
    message_ts: str = "",
    user: str = "",
    actions: list[dict] | None = None,
    fields: list[dict] | None = None,
    token: str | None = None,
    deep_link: str | None = None,
    outcome: str = "",
    question_offset: int | None = None,
) -> dict:
    """Post ``body`` as markdown, with ``deep_link`` on the line after it. Each action
    becomes a button that carries ``token``, so a click answers this notification. Each of
    ``fields`` becomes an input, and the Submit button below them carries ``token``. With
    ``message_ts``, Druks edits that posted message, and ``outcome`` takes the place of its
    buttons and inputs. With ``user``, only that person sees the message.
    ``question_offset`` is where a chat agent's question starts in ``body``. Returns
    Slack's answer, which names the message's channel and ts."""
    link = f"\n{deep_link}" if deep_link else ""
    reply, question = body, ""
    if question_offset:
        reply, question = body[:question_offset], body[question_offset:]
    # Slack refuses markdown longer than this, so a long body loses its end and
    # keeps the link, which opens the full text.
    text = cut_markdown(reply, MARKDOWN_TEXT_LIMIT - len(question) - len(link)) + question + link
    blocks = get_blocks(text, actions=actions, fields=fields, token=token, outcome=outcome)
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
