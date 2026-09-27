import base64
import hashlib
import hmac
import secrets
from contextlib import suppress

from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from druks.accounts.models import Account
from druks.secrets.datastructures import Audience
from druks.secrets.enums import SecretKind
from druks.secrets.models import VaultSecret
from druks.services import Service, ServiceConnectError
from druks.services.exceptions import ServiceNotConnectedError
from druks.settings import load_settings

from .client import TwilioClient
from .constants import VOICE_URL_PATH
from .exceptions import CallsLinkError, TwilioError, TwilioNotFoundError


class Twilio(Service):
    """The Twilio account whose numbers take calls: one connection for each linked number."""

    description = "The Twilio account whose phone numbers Druks answers calls on."
    required = False

    class Settings(BaseModel):
        account_sid: str = Field(title="Account SID")
        auth_token: SecretStr = Field(title="Auth token")

    @classmethod
    async def verify(cls, settings: Settings) -> dict:
        client = TwilioClient(settings.account_sid, settings.auth_token.get_secret_value())
        try:
            await client.get_account()
        except TwilioError as error:
            raise ServiceConnectError(
                "Twilio did not accept this account SID and auth token. Paste them from the "
                "Twilio Console."
            ) from error
        return {}

    @classmethod
    async def is_signed(
        cls, session: AsyncSession, signature: str, url: str, fields: dict[str, str]
    ) -> bool:
        """Whether Twilio signed a request to ``url`` with these form fields."""
        if card := await VaultSecret.lookup(session, cls.secret_kind, Audience.service(cls.slug)):
            payload = url + "".join(f"{name}{fields[name]}" for name in sorted(fields))
            digest = hmac.new(card.secrets["auth_token"].encode(), payload.encode(), hashlib.sha1)
            return hmac.compare_digest(base64.b64encode(digest.digest()).decode(), signature)
        return False

    @classmethod
    async def get_client(cls, session: AsyncSession) -> TwilioClient:
        if card := await VaultSecret.lookup(session, cls.secret_kind, Audience.service(cls.slug)):
            return TwilioClient(card.identity["account_sid"], card.secrets["auth_token"])
        raise ServiceNotConnectedError(cls.slug)

    @classmethod
    async def link(
        cls, session: AsyncSession, owner: Account, *, app: str, sid: str
    ) -> VaultSecret:
        """Save the number and its signing secret, and then send its calls to Druks. When
        Twilio refuses the voice URL, the request's rollback removes the row."""
        base = load_settings().urls.webhook_base
        if not base:
            raise CallsLinkError(
                "Twilio has no address to send calls to. Set urls.webhook_host or urls.endpoint."
            )
        if sid in await cls.list_linked_sids(session):
            raise CallsLinkError("This number is linked. Remove it first.")
        client = await cls.get_client(session)
        number = await client.get_number(sid)
        # A new row for each link, so a removed number keeps its calls.
        connection = VaultSecret(
            kind=SecretKind.SESSION,
            audience=Audience.service(cls.slug),
            account_id=owner.id,
            identity={"app": app, "number": number["phone_number"], "sid": sid},
            secrets={"signing_secret": secrets.token_urlsafe(32)},
        )
        session.add(connection)
        await session.flush()
        await client.set_voice_url(sid, f"{base}{VOICE_URL_PATH}")
        return connection

    @classmethod
    async def unlink(cls, session: AsyncSession, connection: VaultSecret) -> None:
        """Send the number's calls nowhere. The row keeps its facts and its calls."""
        client = await cls.get_client(session)
        # A number that the account no longer holds sends no calls to Druks.
        with suppress(TwilioNotFoundError):
            await client.set_voice_url(connection.identity["sid"], "")
        await connection.revoke("user")

    @classmethod
    async def list_connections(cls, session: AsyncSession, app: str) -> list[VaultSecret]:
        """An app's numbers, removed ones included."""
        return list(
            await session.scalars(
                select(VaultSecret)
                .where(
                    VaultSecret.kind == SecretKind.SESSION,
                    VaultSecret.audience == Audience.service(cls.slug),
                    VaultSecret.identity["app"].astext == app,
                )
                .order_by(VaultSecret.created_at, VaultSecret.id)
            )
        )

    @classmethod
    async def get_for_number(cls, session: AsyncSession, number: str) -> VaultSecret | None:
        """The live connection that holds a phone number."""
        return await session.scalar(
            select(VaultSecret).where(
                VaultSecret.kind == SecretKind.SESSION,
                VaultSecret.audience == Audience.service(cls.slug),
                VaultSecret.revoked_at.is_(None),
                VaultSecret.identity["number"].astext == number,
            )
        )

    @classmethod
    async def get_connection(cls, session: AsyncSession, connection_id: str) -> VaultSecret | None:
        """A linked number. A removed one stays readable."""
        connection = await session.get(VaultSecret, connection_id)
        if (
            connection
            and connection.kind == SecretKind.SESSION
            and connection.audience == Audience.service(cls.slug)
        ):
            return connection
        return

    @classmethod
    async def list_linked_sids(cls, session: AsyncSession) -> set[str]:
        """The Twilio SIDs of the numbers that live connections hold."""
        return set(
            await session.scalars(
                select(VaultSecret.identity["sid"].astext).where(
                    VaultSecret.kind == SecretKind.SESSION,
                    VaultSecret.audience == Audience.service(cls.slug),
                    VaultSecret.revoked_at.is_(None),
                )
            )
        )

    @classmethod
    async def list_unlinked_numbers(cls, session: AsyncSession) -> list[dict]:
        """The account's numbers that take calls and that no live connection holds."""
        linked = await cls.list_linked_sids(session)
        client = await cls.get_client(session)
        return [number for number in await client.list_numbers() if number["sid"] not in linked]


class Voice(Service):
    description = "The voice model that answers phone calls, and its key."
    required = False

    class Settings(BaseModel):
        model: str = Field(
            title="Model",
            description=(
                "The vendor and the model, for example openai/gpt-realtime-mini or "
                "google/gemini-2.5-flash-native-audio."
            ),
        )
        key: SecretStr = Field(title="Key")
        voice: str = Field(
            "",
            title="Voice",
            description="The speaking voice, for example marin or Kore. Empty uses the default.",
        )
