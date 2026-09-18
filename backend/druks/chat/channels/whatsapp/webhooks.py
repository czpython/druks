import hashlib
import hmac
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from dbos import DBOS
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from druks.chat.bots.service import route_message
from druks.chat.enums import ConversationSource
from druks.chat.models import Conversation, Message
from druks.chat.service import deliver
from druks.db import db_session
from druks.files.datastructures import File
from druks.webhooks import Webhook

from .services import Waha


class WahaEvents(Webhook):
    """Verifies the session's HMAC, then saves the linked number's messages in chat."""

    provider = "waha"
    category = "events"

    async def request_is_authentic(self) -> bool:
        # Each linked number signs with its own webhook secret, found by the session
        # that the event names.
        self.connection = await Waha.get_for_name(db_session(), self.data["session"])
        if self.connection:
            secret = self.connection.secrets["webhook_secret"].encode()
            expected = hmac.new(secret, self.raw_body, hashlib.sha512).hexdigest()
            return hmac.compare_digest(expected, self.request.headers.get("X-Webhook-Hmac", ""))
        return False

    def delivery_key(self) -> str:
        return self.data["id"]

    def get_action(self) -> str:
        return self.data["event"].replace(".", "_")

    async def on_message_any(self) -> Response:
        """Save the message in the conversation it goes to, and start its turn."""
        session = db_session()
        message = self.data["payload"]
        # WAHA's id reads {fromMe}_{chat}_{id}, and a reply that Druks sent carries the plain id.
        key = message["id"].split("_", 2)[2]
        if not await Message.get_for_source_id(session, key):
            conversation = await route_message(
                session,
                self.connection,
                self.get_user(),
                body=message["body"] or "",
                key=key,
                is_from_phone=message["fromMe"],
                is_self_chat=self.is_self_chat,
            )
            if conversation:
                await self.save_message(session, conversation, key)
        return JSONResponse({"accepted": True})

    async def on_session_status(self) -> Response:
        await Waha.update_status(db_session(), self.connection, self.data)
        return JSONResponse({"accepted": True})

    @property
    def is_self_chat(self) -> bool:
        """Whether the message is in the phone's chat with itself. WhatsApp names that
        chat by the number or by its LID."""
        me = self.data["me"]
        return self.data["payload"]["from"] in (me["id"], me.get("lid"))

    def get_user(self) -> dict[str, str]:
        """The person of the message's chat, as WAHA names them. A person known only by
        a LID has no phone."""
        message = self.data["payload"]
        chat_id = message["from"]
        raw = message.get("_data") or {}
        name = raw.get("pushName") or raw.get("Info", {}).get("PushName") or raw.get("notifyName")
        phone = ""
        if chat_id.endswith("@c.us"):
            phone = "+" + chat_id.removesuffix("@c.us")
        if self.is_self_chat:
            # The phone's chat with itself stays one conversation, under the number.
            chat_id = self.data["me"]["id"]
        elif message["fromMe"]:
            # A message that the phone sent carries the phone's name, not the person's.
            name = ""
        return {
            "source": ConversationSource.WHATSAPP,
            "user_id": chat_id,
            "user_name": name or "",
            "user_phone": phone,
        }

    async def save_message(
        self, session: AsyncSession, conversation: Conversation, key: str
    ) -> None:
        """Save the message and start its turn. A message with no text and no media, such
        as a shared location, gives the agent nothing to read."""
        message = self.data["payload"]
        file = await self.save_media(session, conversation)
        if body := message["body"] or (file.name if file else ""):
            await conversation.create_message(session, body, source_id=key, file=file)
            await session.commit()
            await DBOS.start_workflow_async(deliver, conversation.id)

    async def save_media(self, session: AsyncSession, conversation: Conversation) -> File | None:
        """Keep the message's media as a Druks file. WAHA deletes its copy within minutes."""
        message = self.data["payload"]
        media = message.get("media") or {}
        if message.get("hasMedia") and media.get("url"):
            client = await Waha.get_client(session, conversation.connection)
            # WAHA builds the URL from its own base, which Druks may not reach.
            path = urlsplit(media["url"]).path
            return await File.create(
                name=media.get("filename") or PurePosixPath(path).name,
                content_type=media.get("mimetype") or "application/octet-stream",
                content=await client.download(path),
                app="chat",
                uploaded_by=conversation.account_id,
            )
        return
