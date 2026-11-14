import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs

from dbos import DBOS
from fastapi import HTTPException, status
from fastapi.responses import JSONResponse, Response

from druks.core.services import Slack
from druks.db import db_session
from druks.durable.engine import step_session
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
from druks.services.exceptions import ServiceNotConnectedError
from druks.signals import publish
from druks.webhooks import Webhook

# Slack signs v0:{ts}:{body}; a timestamp outside the window is a replayed capture.
_TIMESTAMP_TOLERANCE_SECONDS = 300

# What a clicked message says in place of its buttons.
CHOICE_MESSAGE = "<@{user}> chose {choice}."


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

    async def on_block_actions(self) -> Response:
        try:
            action_id = self.data["actions"][0]["action_id"]
            channel = self.data["channel"]["id"]
            message = self.data["message"]
            message_ts = message["ts"]
            user = self.data["user"]["id"]
        except (TypeError, KeyError, IndexError) as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Malformed Slack payload.") from error
        try:
            token, choice_id = decode_button(action_id)
        except MalformedButtonError as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown button.") from error
        # A click that cannot answer still gets a 200: nothing a retry can fix, so
        # acknowledge instead of feeding Slack's retry loop. Corruption propagates as
        # the logged 500 it is. What Slack shows after a click starts at once and is
        # not awaited: a slow or refused post cannot fail the click, and a queue would
        # add up to a second before the person sees it.
        try:
            notification = await respond_to_notification(
                db_session(), token, {"control": choice_id}, user_id=user
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
        await DBOS.start_workflow_async(
            replace_buttons,
            notification.id,
            channel=channel,
            message_ts=message_ts,
            outcome=CHOICE_MESSAGE.format(
                user=user, choice=notification.get_action(choice_id)["label"]
            ),
        )
        return JSONResponse({"accepted": True})


# DBOS runs a lone step as a workflow of its own, so the click handler starts these two
# and does not wait for them.
@DBOS.step(name="slack.replace_buttons", retries_allowed=True, max_attempts=5)
async def replace_buttons(
    notification_id: str, *, channel: str, message_ts: str, outcome: str
) -> None:
    """Edit the notification's message in Slack: ``outcome`` takes the place of its
    buttons."""
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
    )


@DBOS.step(name="slack.explain_reason", retries_allowed=True, max_attempts=5)
async def explain_reason(channel: str, *, user: str, reason: str, thread_ts: str) -> None:
    """Show ``reason`` to the person who clicked, and to nobody else."""
    async with step_session():
        client = await Slack.get_client()
    await deliver(client, channel, reason, user=user, thread_ts=thread_ts)


class SlackEvents(SlackWebhook):
    """Publishes each person's message, and the bot's join to a room, as facts. It
    knows nothing about conversations."""

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
        """Publish a person's message, and the bot's own join to a room. Edits, deletes,
        other joins, hidden events, the bot's own messages, and people from other
        workspaces are not facts for anyone."""
        event = self.data["event"]
        bot_user_id = (await Slack.get()).identity["bot_user_id"]
        is_from_person = (
            event["type"] == "message"
            and (not event.get("subtype") or event["subtype"] in ("file_share", "thread_broadcast"))
            and not event.get("hidden")
            and not event.get("bot_id")
            and event.get("user") != bot_user_id
            and event.get("user_team", self.data["team_id"]) == self.data["team_id"]
        )
        if is_from_person:
            await publish("slack.message", message=event)
        elif event["type"] == "member_joined_channel" and event["user"] == bot_user_id:
            await publish("slack.bot_joined", room=event["channel"])
        return JSONResponse({"accepted": True})
