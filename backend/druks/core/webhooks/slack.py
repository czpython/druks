import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs

from fastapi import HTTPException, status
from fastapi.responses import JSONResponse, Response

from druks.core.services import Slack
from druks.db import db_session
from druks.notifications.buttons import decode_button
from druks.notifications.exceptions import (
    AlreadyAcknowledgedError,
    InvalidChoiceError,
    MalformedButtonError,
    StaleRoundError,
    UnknownTokenError,
)
from druks.notifications.services import respond_to_notification
from druks.services.exceptions import ServiceNotConnectedError
from druks.signals import publish
from druks.webhooks import Webhook

# Slack signs v0:{ts}:{body}; a timestamp outside the window is a replayed capture.
_TIMESTAMP_TOLERANCE_SECONDS = 300


def verify_slack_signature(
    raw_body: bytes,
    signature: str | None,
    timestamp: str | None,
    secret: str,
) -> None:
    # HMAC-SHA256 over v0:{ts}:{body}; raises HTTPException directly so
    # request_is_authentic can call it inline.
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
    """A delivery from the installation's Slack app, signed with the card's secret."""

    abstract = True
    provider = "slack"

    async def request_is_authentic(self) -> bool:
        try:
            self.card = await Slack.get()
        except ServiceNotConnectedError as error:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(error)) from error
        verify_slack_signature(
            self.raw_body,
            self.request.headers.get("x-slack-signature"),
            self.request.headers.get("x-slack-request-timestamp"),
            self.card.secrets["signing_secret"],
        )
        return True


class SlackInteractivity(SlackWebhook):
    # A button click on a delivered notification: decode the action_id back to
    # (token, choice) and route it through the same respond core as the HTTP
    # rail. Slack sends no stable interactivity id, so the base get_delivery_key
    # stays None — the DBOS round key is the one-answer guard.
    category = "interactivity"

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
        except (TypeError, KeyError, IndexError) as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Malformed Slack payload.") from error
        try:
            token, choice_id = decode_button(action_id)
        except MalformedButtonError as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown button.") from error
        try:
            await respond_to_notification(db_session(), token, {"control": choice_id})
        except (
            UnknownTokenError,
            AlreadyAcknowledgedError,
            StaleRoundError,
            InvalidChoiceError,
        ) as error:
            # A click on a dead round (unknown, acknowledged, stale, informational):
            # nothing a retry can fix, so acknowledge instead of feeding Slack's
            # retry loop; the app shows the authoritative state. Corruption
            # propagates as the logged 500 it is.
            self.log_ignored(event="block_actions", reason=type(error).__name__)
            return JSONResponse({"accepted": False})
        return JSONResponse({"accepted": True})


class SlackEvents(SlackWebhook):
    """Publishes each person's message as a fact. It knows nothing about
    conversations."""

    category = "events"

    def get_delivery_key(self) -> str | None:
        """A message's room and timestamp. A timestamp is unique in one room only."""
        if self.data["type"] == "event_callback":
            event = self.data["event"]
            return f"{event['channel']}:{event['ts']}"
        return

    def get_action(self) -> str:
        return self.data["type"]

    async def on_url_verification(self) -> Response:
        return JSONResponse({"challenge": self.data["challenge"]})

    async def on_event_callback(self) -> Response:
        """Publish a person's message. Edits, deletes, joins, hidden events, the bot's
        own messages, and people from other workspaces are not facts for anyone."""
        event = self.data["event"]
        is_from_person = (
            (not event.get("subtype") or event["subtype"] in ("file_share", "thread_broadcast"))
            and not event.get("hidden")
            and not event.get("bot_id")
            and event.get("user") != self.card.identity["bot_user_id"]
            and event.get("user_team", self.data["team_id"]) == self.data["team_id"]
        )
        if is_from_person:
            await publish("slack.message", message=event)
        return JSONResponse({"accepted": True})
