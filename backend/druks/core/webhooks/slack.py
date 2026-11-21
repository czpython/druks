import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs

from dbos import DBOS
from fastapi import HTTPException, status
from fastapi.responses import JSONResponse, Response

from druks.chat.forms import get_answer
from druks.core.services import Slack
from druks.db import db_session
from druks.durable.engine import step_session
from druks.exceptions import ObjectNotFound
from druks.notifications.buttons import decode_button
from druks.notifications.constants import CLOSED_MESSAGE
from druks.notifications.delivery import deliver
from druks.notifications.exceptions import (
    AlreadyAcknowledgedError,
    AnswerNotAllowedError,
    InvalidChoiceError,
    MalformedButtonError,
    StaleRoundError,
    UnknownTokenError,
)
from druks.notifications.models import Notification
from druks.notifications.services import respond_to_notification
from druks.secrets.models import VaultSecret
from druks.services.exceptions import ServiceNotConnectedError
from druks.signals import publish
from druks.webhooks import Webhook

# Slack signs v0:{ts}:{body}; a timestamp outside the window is a replayed capture.
_TIMESTAMP_TOLERANCE_SECONDS = 300

# What a clicked message says in place of its buttons.
CHOICE_MESSAGE = "<@{user}> chose {choice}."

# What a submitted form's message says in place of its inputs.
ANSWER_MESSAGE = "<@{user}> answered:\n{answers}"


def verify_slack_signature(
    raw_body: bytes,
    signature: str | None,
    timestamp: str | None,
    secret: str,
) -> None:
    if not secret:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Webhook secret is not set.",
        )
    if not signature or not timestamp:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Missing webhook signature.",
        )
    try:
        age_seconds = abs(time.time() - int(timestamp))
    except ValueError as error:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid webhook signature.",
        ) from error
    if age_seconds > _TIMESTAMP_TOLERANCE_SECONDS:
        # A replayed capture: the signature may be real but the request is old.
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Stale webhook timestamp.",
        )
    base = b"v0:" + timestamp.encode() + b":" + raw_body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid webhook signature.",
        )


class SlackWebhook(Webhook):
    abstract = True
    provider = "slack"

    async def request_is_authentic(self) -> bool:
        try:
            signing_secret = (await Slack.get()).secrets["signing_secret"]
        except ServiceNotConnectedError as error:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(error)) from error
        verify_slack_signature(
            self.raw_body,
            self.request.headers.get("x-slack-signature"),
            self.request.headers.get("x-slack-request-timestamp"),
            signing_secret,
        )
        return True


class SlackInteractivity(SlackWebhook):
    category = "interactivity"

    def get_delivery_key(self) -> str:
        # Slack gives an interaction no ID. A retry sends the same bytes.
        return "interaction:" + hashlib.sha256(self.raw_body).hexdigest()

    def get_data(self) -> dict:
        # Interactivity arrives form-encoded as payload=<json>.
        try:
            return json.loads(parse_qs(self.raw_body.decode())["payload"][0])
        except (UnicodeDecodeError, KeyError, json.JSONDecodeError) as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Malformed Slack payload.") from error

    def get_action(self) -> str:
        try:
            return self.data["type"]
        except (TypeError, KeyError) as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Malformed Slack payload.") from error

    def get_values(self) -> dict:
        """The answers in the inputs of the clicked message, by field name, in the shapes
        that a form's fields read. Slack keys the state of an input by its block, and Druks
        names the block of a form's input for its field. A message without inputs has no
        state."""
        values = {}
        for block_id, inputs in self.data.get("state", {}).get("values", {}).items():
            for element in inputs.values():
                match element["type"]:
                    case "plain_text_input":
                        values[block_id] = element.get("value") or ""
                    case "static_select" | "radio_buttons":
                        values[block_id] = (element.get("selected_option") or {}).get("value", "")
                    case "multi_static_select":
                        selected = element.get("selected_options") or []
                        values[block_id] = [option["value"] for option in selected]
                    case "checkboxes":
                        values[block_id] = bool(element.get("selected_options"))
        return values

    async def on_block_actions(self) -> Response:
        try:
            action_type = self.data["actions"][0]["type"]
            action_id = self.data["actions"][0]["action_id"]
            channel = self.data["channel"]["id"]
            message = self.data["message"]
            message_ts = message["ts"]
            user = self.data["user"]["id"]
            values = self.get_values()
        except (TypeError, KeyError, IndexError, AttributeError) as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Malformed Slack payload.") from error
        if action_type != "button":
            # A change in an input of a form answers nothing. Its Submit button does.
            self.log_ignored(event="block_actions", reason="input_changed")
            return JSONResponse({"accepted": False})
        try:
            token, choice_id = decode_button(action_id)
        except MalformedButtonError as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown button.") from error
        # The clicker's account, found in the workspace that Druks answers in, as in chat.
        account_id = await VaultSecret.get_owner_id(
            db_session(),
            Slack.Authentication.get_authority((await Slack.get()).identity["team_id"]),
            user,
        )
        # A click that cannot answer still gets a 200: nothing a retry can fix, so
        # acknowledge instead of feeding Slack's retry loop. Corruption propagates as
        # the logged 500 it is. What Slack shows after a click starts at once and is
        # not awaited: a slow or refused post cannot fail the click, and a queue would
        # add up to a second before the person sees it.
        try:
            notification = await respond_to_notification(
                db_session(),
                token,
                {"control": choice_id},
                user_id=user,
                account_id=account_id,
                values=values,
            )
        except StaleRoundError as error:
            # The round is over, so the clicked message loses its buttons.
            self.log_ignored(event="block_actions", reason=type(error).__name__)
            notification = await Notification.get_for_token(db_session(), token)
            await DBOS.start_workflow_async(
                replace_buttons,
                notification.id,
                channel=channel,
                message_ts=message_ts,
                outcome=CLOSED_MESSAGE,
            )
            return JSONResponse({"accepted": False})
        except (
            UnknownTokenError,
            AlreadyAcknowledgedError,
            InvalidChoiceError,
            AnswerNotAllowedError,
            ObjectNotFound,
        ) as error:
            # The question can still be open, so the message stays, and only the
            # clicker reads why.
            self.log_ignored(event="block_actions", reason=type(error).__name__)
            await DBOS.start_workflow_async(
                explain_reason,
                channel,
                user=user,
                reason=str(error),
                thread_ts=message.get("thread_ts", ""),
            )
            return JSONResponse({"accepted": False})
        if notification.fields:
            # Escaped, the person's words cannot mention anyone or link anywhere in the message.
            answers = get_answer(notification.fields, values)
            answers = answers.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            outcome = ANSWER_MESSAGE.format(user=user, answers=answers)
        else:
            outcome = CHOICE_MESSAGE.format(
                user=user, choice=notification.get_action(choice_id)["label"]
            )
        await DBOS.start_workflow_async(
            replace_buttons,
            notification.id,
            channel=channel,
            message_ts=message_ts,
            outcome=outcome,
        )
        return JSONResponse({"accepted": True})


# DBOS runs a lone step as a workflow of its own, so the click handler starts these two
# and does not wait for them.
@DBOS.step(name="slack.replace_buttons", retries_allowed=True, max_attempts=5)
async def replace_buttons(
    notification_id: str, *, channel: str, message_ts: str, outcome: str
) -> None:
    """Edit the notification's message in Slack: ``outcome`` takes the place of its
    buttons and inputs."""
    async with step_session() as session:
        notification = await session.get(Notification, notification_id)
        client = await Slack.get_client()
    await deliver(
        client,
        channel,
        notification.body,
        message_ts=message_ts,
        deep_link=notification.deep_link,
        outcome=outcome,
        question_offset=notification.question_offset,
    )


@DBOS.step(name="slack.explain_reason", retries_allowed=True, max_attempts=5)
async def explain_reason(channel: str, *, user: str, reason: str, thread_ts: str) -> None:
    """Show ``reason`` to the person who clicked, and to nobody else."""
    async with step_session():
        client = await Slack.get_client()
    await deliver(client, channel, reason, user=user, thread_ts=thread_ts)


class SlackEvents(SlackWebhook):
    """Publishes each person's message, another bot's tag in a room, and the bot's join
    to a room, as facts. It knows nothing about conversations."""

    category = "events"

    def get_delivery_key(self) -> str | None:
        if self.data["type"] == "event_callback":
            return self.data["event_id"]
        return

    def get_action(self) -> str:
        return self.data["type"]

    async def on_url_verification(self) -> Response:
        return JSONResponse({"challenge": self.data["challenge"]})

    async def on_event_callback(self) -> Response:
        """Publish a person's message, another bot's tag in a room, and the bot's own
        join to a room. Edits, deletes, other joins, hidden events, the bot's own
        messages, other bots' untagged or direct messages, and people from other
        workspaces are not facts for anyone."""
        event = self.data["event"]
        bot_user_id = (await Slack.get()).identity["bot_user_id"]
        # A bot has no Slack account to link and no direct message to answer in, so it
        # reaches the bot only by a tag in a room.
        is_tag = f"<@{bot_user_id}>" in event.get("text", "")
        is_tag_in_room = is_tag and event.get("channel_type") != "im"
        is_fact = (
            event["type"] == "message"
            and (
                not event.get("subtype")
                or event["subtype"] in ("file_share", "thread_broadcast", "bot_message")
            )
            and not event.get("hidden")
            and (not event.get("bot_id") or is_tag_in_room)
            and event.get("user") != bot_user_id
            and event.get("user_team", self.data["team_id"]) == self.data["team_id"]
        )
        if is_fact:
            await publish("slack.message", message=event)
        elif event["type"] == "member_joined_channel" and event["user"] == bot_user_id:
            await publish("slack.bot_joined", room=event["channel"])
        return JSONResponse({"accepted": True})
