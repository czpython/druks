import base64
import hashlib
import hmac
from urllib.parse import parse_qsl

from fastapi import HTTPException, status

from druks.core.services import Twilio
from druks.services.exceptions import ServiceNotConnectedError
from druks.settings import load_settings
from druks.webhooks import Webhook


def verify_twilio_signature(
    *, url: str, fields: dict[str, str], signature: str | None, auth_token: str
) -> None:
    """Refuse a request that Twilio did not sign. Twilio signs the URL and the sorted form
    fields with HMAC-SHA1. It signs a stream's handshake with or without a trailing slash
    on the URL, so either form passes."""
    if not signature:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing webhook signature.")
    signed_fields = "".join(f"{name}{fields[name]}" for name in sorted(fields))
    url = url.rstrip("/")
    for signed_url in (url, f"{url}/"):
        digest = hmac.new(
            key=auth_token.encode(),
            msg=f"{signed_url}{signed_fields}".encode(),
            digestmod=hashlib.sha1,
        ).digest()
        if hmac.compare_digest(base64.b64encode(digest).decode(), signature):
            return
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook signature.")


class TwilioWebhook(Webhook):
    """A request from Twilio, signed with the card's auth token."""

    abstract = True
    provider = "twilio"

    def get_data(self) -> dict[str, str]:
        # Twilio signs every field it posts, the blank ones too.
        return dict(parse_qsl(self.raw_body.decode(), keep_blank_values=True))

    async def request_is_authentic(self) -> bool:
        try:
            card = await Twilio.get()
        except ServiceNotConnectedError as error:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(error)) from error
        verify_twilio_signature(
            url=f"{load_settings().urls.webhook_base}/_external/{self.path}",
            fields=self.data,
            signature=self.request.headers.get("X-Twilio-Signature"),
            auth_token=card.secrets["auth_token"],
        )
        return True
