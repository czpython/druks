import secrets

from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from druks.accounts.enums import AccountKind
from druks.accounts.models import Account
from druks.secrets.enums import IdentityStatus, SecretKind
from druks.secrets.models import VaultSecret
from druks.services import Service
from druks.services.exceptions import ServiceNotConnectedError
from druks.settings import load_settings

from .client import WahaClient
from .constants import SUPPORTED_ENGINES, WAHA_AUDIENCE
from .exceptions import WhatsAppLinkError


class Waha(Service):
    """WAHA's sessions: one for each linked WhatsApp number."""

    description = (
        "The WAHA server Druks reaches WhatsApp through. Druks uses this key only to "
        "create and delete a linked number's session and its session key."
    )
    required = False

    class Settings(BaseModel):
        url: str = Field(title="Address", description="Base URL of the WAHA server.")
        key: SecretStr = Field(title="Key")

    @classmethod
    async def get_client(
        cls, session: AsyncSession, connection: VaultSecret | None = None
    ) -> WahaClient:
        """A client under this card's key, or under a linked number's session key."""
        if card := await VaultSecret.lookup(session, cls.secret_kind, WAHA_AUDIENCE):
            if connection:
                return WahaClient(
                    card.identity["url"], connection.secrets["key"], connection.identity["session"]
                )
            return WahaClient(card.identity["url"], card.secrets["key"])
        raise ServiceNotConnectedError(cls.slug)

    @classmethod
    async def link(cls, session: AsyncSession, owner: Account, *, identity: dict) -> VaultSecret:
        """Create the number's session and its key, save the session, and then write
        its config: WAHA sends events only after the config names the webhook."""
        if await VaultSecret.lookup(session, SecretKind.SESSION, WAHA_AUDIENCE, owner.id):
            raise WhatsAppLinkError("This account has a linked number. Remove it first.")
        base = load_settings().urls.webhook_base
        if not base:
            raise WhatsAppLinkError(
                "WAHA has no address to send events to. Set urls.webhook_host or urls.endpoint."
            )
        card = await cls.get_client(session)
        name = await card.create_session()
        key = await card.create_key(name)
        webhook_secret = secrets.token_urlsafe(32)
        # A new row for each link, so a removed number keeps its facts and its chats.
        connection = VaultSecret(
            kind=SecretKind.SESSION,
            audience=WAHA_AUDIENCE,
            account_id=owner.id,
            identity={**identity, "session": name},
            secrets={
                "key": key["key"],
                "key_id": key["id"],
                "webhook_secret": webhook_secret,
            },
        )
        session.add(connection)
        await session.commit()
        client = await cls.get_client(session, connection)
        await client.configure(
            {
                "webhooks": [
                    {
                        "url": f"{base}/_external/waha/events/",
                        "events": ["message.any", "session.status"],
                        "hmac": {"key": webhook_secret},
                    }
                ],
                "ignore": {"status": True, "groups": True, "channels": True, "broadcast": True},
                # WhatsApp does not ring the phone while a linked client is online, and
                # a NOWEB session marks itself online unless its config says otherwise.
                "noweb": {"markOnline": False},
                # WhatsApp names the linked device "<browserName> (<deviceName>)", and it
                # drops a device name that comes with a browser name it does not know.
                "client": {"deviceName": "Druks", "browserName": "Chrome"},
            }
        )
        return connection

    @classmethod
    async def unlink(cls, session: AsyncSession, connection: VaultSecret, reason: str) -> None:
        """Delete the number's session and its key. The row keeps its facts."""
        card = await cls.get_client(session)
        await card.delete_session(connection.identity["session"])
        await card.delete_key(connection.secrets["key_id"])
        await connection.revoke(reason)

    @classmethod
    async def relink(cls, session: AsyncSession, connection: VaultSecret) -> None:
        """Take a new scan on the same connection, which keeps its chats, its admin, and
        its connected phones. The session teaches Druks the number again when it works."""
        client = await cls.get_client(session, connection)
        await client.logout()
        await client.start()
        connection.identity = {
            key: value
            for key, value in connection.identity.items()
            if key not in ("number", "name", "user_id")
        }
        connection.identity_status = IdentityStatus.UNAVAILABLE

    @classmethod
    async def list_sessions(
        cls, session: AsyncSession, *, app: str, account_id: str
    ) -> list[VaultSecret]:
        """An app's sessions, or else the account's own, removed ones included."""
        query = (
            select(VaultSecret)
            .where(VaultSecret.kind == SecretKind.SESSION, VaultSecret.audience == WAHA_AUDIENCE)
            .order_by(VaultSecret.created_at, VaultSecret.id)
        )
        if app:
            query = query.where(VaultSecret.identity["app"].astext == app)
        else:
            query = query.where(VaultSecret.account_id == account_id)
        return list(await session.scalars(query))

    @classmethod
    async def get_session(
        cls, session: AsyncSession, session_id: str, account_id: str
    ) -> VaultSecret | None:
        """A session the account may manage: an app's, or its own. A removed one stays
        readable."""
        connection = await session.get(VaultSecret, session_id)
        if (
            connection
            and connection.kind == SecretKind.SESSION
            and connection.audience == WAHA_AUDIENCE
            and (connection.account.kind == AccountKind.BOT or connection.account_id == account_id)
        ):
            return connection
        return

    @classmethod
    async def get_for_name(cls, session: AsyncSession, name: str) -> VaultSecret | None:
        """The live session that WAHA names."""
        return await session.scalar(
            select(VaultSecret).where(
                VaultSecret.kind == SecretKind.SESSION,
                VaultSecret.audience == WAHA_AUDIENCE,
                VaultSecret.revoked_at.is_(None),
                VaultSecret.identity["session"].astext == name,
            )
        )

    @classmethod
    async def update_status(
        cls, session: AsyncSession, connection: VaultSecret, event: dict
    ) -> None:
        """Keep whether the session works. Druks refuses a session on an engine it cannot
        read, and a number that another live session holds."""
        if event["payload"]["status"] == "WORKING":
            if event["engine"] not in SUPPORTED_ENGINES:
                await cls.unlink(session, connection, "unsupported_engine")
                return
            me = event["me"]
            number = "+" + me["id"].partition("@")[0]
            holding_connection_id = await session.scalar(
                select(VaultSecret.id).where(
                    VaultSecret.kind == SecretKind.SESSION,
                    VaultSecret.audience == WAHA_AUDIENCE,
                    VaultSecret.revoked_at.is_(None),
                    VaultSecret.identity["number"].astext == number,
                    VaultSecret.id != connection.id,
                )
            )
            if holding_connection_id:
                await cls.unlink(session, connection, "number_already_linked")
                return
            connection.identity = {
                **connection.identity,
                "number": number,
                "name": me.get("pushName") or "",
                "user_id": me["id"],
            }
            connection.identity_status = IdentityStatus.RESOLVED
        else:
            connection.identity_status = IdentityStatus.UNAVAILABLE
