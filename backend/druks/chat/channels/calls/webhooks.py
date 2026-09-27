import base64
import hashlib
import hmac
import time
from datetime import datetime
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from fastapi.responses import JSONResponse, Response
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from druks.chat.enums import ConversationSource, MessageRole, MessageState
from druks.chat.models import Conversation, Message
from druks.chat.service import get_agent
from druks.db import db_session
from druks.mcp.constants import BEARER_PREFIX
from druks.mcp.inbound import get_druks_account_token, get_druks_mcp_server
from druks.redis import get_client
from druks.secrets.models import VaultSecret
from druks.settings import load_settings
from druks.webhooks import Webhook

from .constants import (
    CALL_PROMPT,
    CALL_TOKEN_SECONDS,
    CALLS_KEY_NAME,
    MAX_CALL_SECONDS,
    NO_SPEECH_SECONDS,
    PICKUP_UTTERANCES,
    VOICE_URL_PATH,
)
from .services import Twilio, Voice


def get_stream_url(token: str) -> str:
    """Where Twilio streams a call's audio: the voice server, behind the webhook host."""
    return f"wss://{urlsplit(load_settings().urls.webhook_base).netloc}/_voice/calls/{token}"


def sign_claim(connection: VaultSecret, claim: str) -> str:
    secret = connection.secrets["signing_secret"].encode()
    digest = hmac.new(secret, claim.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def get_call_token(connection: VaultSecret, conversation: Conversation) -> str:
    """The call's conversation and an expiry, signed with the number's secret. Druks keeps
    no copy: it checks a token by signing its claim again."""
    claim = f"{conversation.id}.{int(time.time()) + CALL_TOKEN_SECONDS}"
    return f"{claim}.{sign_claim(connection, claim)}"


async def get_call(session: AsyncSession, token: str) -> Conversation | None:
    """The call that a valid token names, until the token expires."""
    claim, _, signature = token.rpartition(".")
    conversation_id, _, expires_at = claim.partition(".")
    conversation = await session.get(Conversation, conversation_id)
    if (
        conversation
        and conversation.source == ConversationSource.CALLS
        and conversation.connection.is_live
        and hmac.compare_digest(signature, sign_claim(conversation.connection, claim))
        and int(expires_at) > time.time()
    ):
        return conversation
    return


class TwilioCalls(Webhook):
    """Twilio's call to a linked number, answered with TwiML that streams the audio to the
    voice server."""

    provider = "twilio"
    category = "calls"

    def get_data(self) -> dict[str, str]:
        # Twilio signs every field it posts, the blank ones too.
        return dict(parse_qsl(self.raw_body.decode(), keep_blank_values=True))

    def get_delivery_key(self) -> str:
        return self.data["CallSid"]

    def get_action(self) -> str:
        return "call"

    async def request_is_authentic(self) -> bool:
        url = f"{load_settings().urls.webhook_base}{VOICE_URL_PATH}"
        signature = self.request.headers.get("X-Twilio-Signature", "")
        return await Twilio.is_signed(db_session(), signature, url, self.data)

    async def on_call(self) -> Response:
        """Start the caller's conversation for the call, and tell Twilio to stream its audio
        to the voice server. Twilio strips a query string from a stream URL, so the call
        token rides in the path."""
        session = db_session()
        if connection := await Twilio.get_for_number(session, self.data["To"]):
            conversation = await Conversation.get_or_create_for_user(
                session,
                connection,
                connection.account_id,
                source=ConversationSource.CALLS,
                user_id=self.data["From"],
                user_name="",
                user_phone=self.data["From"],
                thread_id=self.data["CallSid"],
            )
            stream_url = get_stream_url(get_call_token(connection, conversation))
            return Response(
                f'<Response><Connect><Stream url="{stream_url}"/></Connect><Hangup/></Response>',
                media_type="text/xml",
            )
        return Response("<Response><Reject/></Response>", media_type="text/xml")


class VoiceEvents(Webhook):
    """The voice server's requests for a call. Each one carries the call's token."""

    provider = "voice"
    category = "events"

    def get_action(self) -> str:
        return self.data["action"]

    async def request_is_authentic(self) -> bool:
        session = db_session()
        self.conversation = await get_call(session, self.data["token"])
        if self.conversation and self.get_action() == "pickup":
            # The voice server forwards the signature of Twilio's stream handshake. Twilio
            # signs some handshakes with a trailing slash on the URL.
            url = get_stream_url(self.data["token"])
            signature = self.data["signature"]
            return await Twilio.is_signed(session, signature, url, {}) or await Twilio.is_signed(
                session, signature, f"{url}/", {}
            )
        return bool(self.conversation)

    async def on_pickup(self) -> Response:
        """Hand the voice server what the call needs, once: the prompt, the caller's
        facts, the Bot's key, the Voice card, and the timers. The run outcomes that the
        facts carry count as read."""
        session = db_session()
        conversation = self.conversation
        # The key holds no secret. It only marks the call as picked up.
        if await get_client().set(
            f"calls:{conversation.id}:pickup", "1", nx=True, ex=CALL_TOKEN_SECONDS
        ):
            _, prompt, tools = await get_agent(session, conversation)
            key = await get_druks_account_token(
                session, conversation.account_id, tools, name=CALLS_KEY_NAME
            )
            voice = await Voice.get()
            outcomes = await self.list_run_outcomes(session)
            facts = {
                "caller": conversation.user_phone,
                "number": conversation.connection.identity["number"],
                "local_time": datetime.now(ZoneInfo(load_settings().timezone)).isoformat(),
                "earlier_lines": await self.list_earlier_lines(session),
                "run_outcomes": [outcome.body for outcome in outcomes],
            }
            for outcome in outcomes:
                await outcome.mark_delivered(session)
            for conversation_id in {outcome.conversation_id for outcome in outcomes}:
                earlier_call = await session.get(Conversation, conversation_id)
                await earlier_call.end_turn(session, MessageState.REPLIED)
            return JSONResponse(
                {
                    "prompt": f"{prompt}\n\n{CALL_PROMPT}",
                    "facts": facts,
                    "mcp": {
                        "url": get_druks_mcp_server(allowed_tools=()).url,
                        "bearer": key.secrets["value"].removeprefix(BEARER_PREFIX),
                        "conversation": conversation.id,
                    },
                    "model": voice.identity["model"],
                    "key": voice.secrets["key"],
                    "voice": voice.identity["voice"],
                    "limits": {
                        "max_call_seconds": MAX_CALL_SECONDS,
                        "no_speech_seconds": NO_SPEECH_SECONDS,
                    },
                }
            )
        raise HTTPException(409, "The voice server picked up this call already.")

    @property
    def earlier_calls(self) -> Select:
        """The caller's other calls to this number."""
        return select(Conversation.id).where(
            Conversation.connection_id == self.conversation.connection_id,
            Conversation.user_id == self.conversation.user_id,
            Conversation.id != self.conversation.id,
        )

    async def list_earlier_lines(self, session: AsyncSession) -> list[dict]:
        """The last lines of the caller's earlier calls, oldest first."""
        lines = await session.scalars(
            select(Message)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(Message.conversation_id.in_(self.earlier_calls), ~Message.is_internal)
            .order_by(Conversation.created_at.desc(), Message.source_id.desc())
            .limit(PICKUP_UTTERANCES)
        )
        return [
            {
                "role": line.role,
                "text": line.transcript or line.body,
                "said_at": line.created_at.isoformat(),
            }
            for line in reversed(list(lines))
        ]

    async def list_run_outcomes(self, session: AsyncSession) -> list[Message]:
        """The internal messages that wait on the caller's earlier calls: what Druks
        reported there after those calls ended."""
        return list(
            await session.scalars(
                select(Message)
                .where(
                    Message.conversation_id.in_(self.earlier_calls),
                    Message.is_internal,
                    Message.state == MessageState.PENDING,
                )
                .order_by(Message.created_at, Message.id)
            )
        )

    async def on_utterance(self) -> Response:
        """Save a line of the call under its sequence. The voice server numbers the lines
        in the order they were said, and posts them in that order. A caller line waits
        for the assistant's next line."""
        session = db_session()
        conversation = self.conversation
        words = self.data["text"]
        # Zero-padded, so a call's lines sort by their source ids.
        source_id = f"{conversation.thread_id}:{int(self.data['sequence']):08d}"
        if MessageRole(self.data["role"]) == MessageRole.USER:
            line = await conversation.create_message(session, "", source_id=source_id)
            line.transcript = words
            await line.mark_delivered(session)
        else:
            caller_line = await session.scalar(
                select(Message)
                .where(
                    Message.conversation_id == conversation.id,
                    Message.role == MessageRole.USER,
                    Message.source_id < source_id,
                )
                .order_by(Message.source_id.desc())
                .limit(1)
            )
            await conversation.create_message(
                session,
                words,
                role=MessageRole.ASSISTANT,
                reply_to=caller_line,
                source_id=source_id,
            )
            await conversation.end_turn(session, MessageState.REPLIED)
        return JSONResponse({"accepted": True})

    async def on_ended(self) -> Response:
        """Close a caller line that nobody answered."""
        await self.conversation.end_turn(db_session(), MessageState.INTERRUPTED)
        return JSONResponse({"accepted": True})
