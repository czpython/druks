import hashlib
import hmac
import re
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from dbos import DBOS
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from druks.chat.bots.service import get_thread_conversation, route_message
from druks.chat.enums import ConversationSource
from druks.chat.models import Conversation, Message
from druks.chat.service import deliver
from druks.db import db_session
from druks.files.datastructures import File
from druks.webhooks import Webhook

from .services import Waha


def get_phone(chat_id: str) -> str:
    """The phone number in a chat ID. A LID holds none."""
    if chat_id.endswith("@c.us"):
        return "+" + chat_id.removesuffix("@c.us")
    return ""


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

    def get_delivery_key(self) -> str:
        return self.data["id"]

    def get_action(self) -> str:
        return self.data["event"].replace(".", "_")

    async def on_message_any(self) -> Response:
        """Save the message in the conversation it goes to, and start its turn."""
        session = db_session()
        message = self.data["payload"]
        # WAHA's id reads {fromMe}_{chat}_{id}, followed by _{participant} in a group, and a
        # reply that Druks sent carries the plain id.
        key = message["id"].split("_")[2]
        if not await Message.get_for_source_id(session, key):
            if message["from"].endswith("@g.us"):
                conversation = await self.get_group_conversation(session)
            else:
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

    async def get_group_conversation(self, session: AsyncSession) -> Conversation | None:
        """The conversation of a group message, or none. Only a message that addresses
        the Bot of an open number reaches it, and never one that the number's phone sent.
        The admin is the sender whose phone number is the admin's."""
        message = self.data["payload"]
        admin = self.connection.identity.get("admin")
        if admin and self.is_addressed and not message["fromMe"]:
            client = await Waha.get_client(session, self.connection)
            sender = await client.get_phone_chat_id(message["participant"])
            admin_chat_id = await client.get_phone_chat_id(admin.get("user_id", ""))
            user = {
                "source": ConversationSource.WHATSAPP,
                "user_id": message["participant"],
                "user_name": self.sender_name,
                "user_phone": get_phone(sender),
                "thread_id": message["from"],
            }
            return await get_thread_conversation(
                session, self.connection, user, is_from_admin=sender == admin_chat_id
            )
        return

    async def on_session_status(self) -> Response:
        await Waha.update_status(db_session(), self.connection, self.data)
        return JSONResponse({"accepted": True})

    @property
    def is_self_chat(self) -> bool:
        """Whether the message is in the phone's chat with itself. WhatsApp names that
        chat by the number or by its LID."""
        me = self.data["me"]
        return self.data["payload"]["from"] in (me["id"], me.get("lid"))

    @property
    def own_ids(self) -> list[str]:
        """The number's chat IDs: its phone number, and its LID when WhatsApp gave it one."""
        me = self.data["me"]
        return [own_id for own_id in (me["id"], me.get("lid")) if own_id]

    @property
    def own_tag(self) -> re.Pattern:
        """The number's tag in a message: @ and its phone number, or @ and its LID."""
        return re.compile(
            "|".join(rf"@{re.escape(own_id.partition('@')[0])}\b" for own_id in self.own_ids)
        )

    @property
    def is_addressed(self) -> bool:
        """Whether the message tags the number or replies to one of its messages."""
        message = self.data["payload"]
        reply_to = message.get("replyTo") or {}
        return bool(self.own_tag.search(message["body"] or "")) or (
            reply_to.get("participant") in self.own_ids
        )

    @property
    def sender_name(self) -> str:
        raw = self.data["payload"].get("_data") or {}
        return (
            raw.get("pushName")
            or raw.get("Info", {}).get("PushName")
            or raw.get("notifyName")
            or ""
        )

    @property
    def text(self) -> str:
        """The message's text without the number's tag, which the agent would take for the
        person it answers."""
        return self.own_tag.sub("", self.data["payload"]["body"] or "").strip()

    def get_user(self) -> dict[str, str]:
        """The person of the message's chat, as WAHA names them. A person known only by
        a LID has no phone."""
        message = self.data["payload"]
        chat_id = message["from"]
        name = self.sender_name
        phone = get_phone(chat_id)
        if self.is_self_chat:
            # The phone's chat with itself stays one conversation, under the number.
            chat_id = self.data["me"]["id"]
        elif message["fromMe"]:
            # A message that the phone sent carries the phone's name, not the person's.
            name = ""
        return {
            "source": ConversationSource.WHATSAPP,
            "user_id": chat_id,
            "user_name": name,
            "user_phone": phone,
            "thread_id": "",
        }

    async def save_message(
        self, session: AsyncSession, conversation: Conversation, key: str
    ) -> None:
        """Save the message and start its turn. A message with no text and no media, such
        as a shared location, gives the agent nothing to read."""
        body = self.text
        file = await self.save_media(session, conversation)
        if body or file:
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
