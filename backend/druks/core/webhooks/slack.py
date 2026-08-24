import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs

from fastapi import HTTPException, status
from fastapi.responses import JSONResponse, Response

from druks.notifications.buttons import decode_button
from druks.notifications.exceptions import (
    AlreadyAcknowledgedError,
    InvalidChoiceError,
    MalformedButtonError,
    StaleRoundError,
    UnknownTokenError,
)
from druks.notifications.services import respond_to_notification
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


class SlackInteractivity(Webhook):
    # A button click on a delivered notification: decode the action_id back to
    # (token, choice) and route it through the same respond core as the HTTP
    # rail. Slack sends no stable interactivity id, so the base delivery_key
    # stays None — the DBOS round key is the one-answer guard.
    provider = "slack"
    category = "interactivity"

    async def request_is_authentic(self) -> bool:
        verify_slack_signature(
            self.raw_body,
            self.request.headers.get("x-slack-signature"),
            self.request.headers.get("x-slack-request-timestamp"),
            self.settings.slack_signing_secret,
        )
        return True

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
            await respond_to_notification(token, {"control": choice_id})
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
