import hashlib
import hmac
import re
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import vobject
from dbos import DBOS
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from druks.apps.loader import get_app
from druks.chat.bots.service import get_thread_conversation, route_message
from druks.chat.enums import BotAccess, ConversationSource
from druks.chat.models import Conversation, Message
from druks.chat.service import deliver
from druks.db import db_session
from druks.files.datastructures import File
from druks.webhooks import Webhook

from .channel import get_phone, get_sender_name, get_source_id
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

    def get_delivery_key(self) -> str:
        return self.data["id"]

    def get_action(self) -> str:
        return self.data["event"].replace(".", "_")

    async def on_message_any(self) -> Response:
        """Save the message in the conversation it goes to, and start its turn."""
        session = db_session()
        message = self.data["payload"]
        key = get_source_id(message["id"])
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
        the Bot reaches it, and never one that the number's phone sent. A paired phone
        and the admin write under their own account."""
        message = self.data["payload"]
        if self.is_addressed and not message["fromMe"]:
            identity = self.connection.identity
            if get_app(identity["app"]).bot.access == BotAccess.PRIVATE:
                accounts = identity["operators"]
            else:
                accounts = {identity["admin"].get("user_id", ""): identity["admin"]["account_id"]}
            client = await Waha.get_client(session, self.connection)
            # A group can name a member by a LID, so Druks compares phone numbers.
            accounts_by_phone = {
                await client.get_phone_chat_id(user_id): account_id
                for user_id, account_id in accounts.items()
            }
            sender = await client.get_phone_chat_id(message["participant"])
            user = {
                "source": ConversationSource.WHATSAPP,
                "user_id": message["participant"],
                "user_name": get_sender_name(message),
                "user_phone": get_phone(sender),
                "thread_id": message["from"],
            }
            conversation = await get_thread_conversation(
                session, self.connection, user, sender_account_id=accounts_by_phone.get(sender)
            )
            if conversation and not conversation.title:
                conversation.title = await client.get_group_name(message["from"])
            return conversation
        return

    async def on_session_status(self) -> Response:
        await Waha.update_status(db_session(), self.connection, self.data)
        return JSONResponse({"accepted": True})

    @property
    def is_self_chat(self) -> bool:
        """Whether the message is in the phone's chat with itself. WhatsApp names that
        chat by the number or by its LID."""
        return self.data["payload"]["from"] in self.own_ids

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

    def get_text(self, message: dict) -> str:
        """A message's text without the number's tag, which the agent would take for the
        person it answers. A line for each shared contact follows it."""
        typed_text = self.own_tag.sub("", message["body"] or "").strip()
        contacts = [f"Shared contact: {contact}" for contact in self.get_contacts(message)]
        return "\n".join(filter(None, [typed_text, *contacts]))

    def get_contacts(self, message: dict) -> list[str]:
        """The name and phone numbers on each contact card that a message shares. WhatsApp
        marks a number on WhatsApp with its waid, which holds the country code that a saved
        number can omit."""
        vcards = "\n".join(message.get("vCards") or [])
        contacts = []
        for card in vobject.readComponents(vcards):
            phones = [
                f"+{tel.params['WAID'][0]}" if "WAID" in tel.params else tel.value
                for tel in card.contents.get("tel", [])
            ]
            contacts.append(", ".join([card.fn.value, *phones]))
        return contacts

    def get_user(self) -> dict[str, str]:
        """The person of the message's chat, as WAHA names them. A person known only by
        a LID has no phone."""
        message = self.data["payload"]
        chat_id = message["from"]
        name = get_sender_name(message)
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
        body = self.get_text(self.data["payload"])
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
