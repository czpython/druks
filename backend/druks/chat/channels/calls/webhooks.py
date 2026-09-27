import base64
import hashlib
import hmac
import time
from datetime import datetime
from operator import attrgetter
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from fastapi.responses import JSONResponse, Response
from sqlalchemy import Select, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from druks.chat.enums import ConversationSource, MessageRole, MessageState
from druks.chat.models import Conversation, Message
from druks.chat.service import get_agent
from druks.core.services import Twilio
from druks.core.webhooks.twilio import TwilioWebhook, verify_twilio_signature
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
    EARLIER_LINES,
    MAX_CALL_SECONDS,
    NO_SPEECH_SECONDS,
)
from .services import Voice


def get_stream_url(token: str) -> str:
    """Where Twilio streams a call's audio: the calls server, behind the webhook host."""
    return f"wss://{urlsplit(load_settings().urls.webhook_base).netloc}/_calls/{token}"


def sign_claim(connection: VaultSecret, claim: str) -> str:
    digest = hmac.new(
        key=connection.secrets["signing_secret"].encode(),
        msg=claim.encode(),
        digestmod=hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def get_call_token(conversation: Conversation) -> str:
    """The call's conversation and an expiry, signed with the number's secret. Druks keeps
    no copy. To check a token, it signs the claim again."""
    claim = f"{conversation.id}.{int(time.time()) + CALL_TOKEN_SECONDS}"
    return f"{claim}.{sign_claim(conversation.connection, claim)}"


async def get_call(session: AsyncSession, token: str) -> Conversation | None:
    """The call that a valid token names, until the token expires."""
    claim, _, signature = token.rpartition(".")
    conversation_id, _, expires_at = claim.partition(".")
    conversation = await session.get(Conversation, conversation_id)
    if (
        conversation
        and conversation.source == ConversationSource.CALL
        and conversation.connection.is_live
        and hmac.compare_digest(signature, sign_claim(conversation.connection, claim))
        and int(expires_at) > time.time()
    ):
        return conversation
    return


class TwilioCalls(TwilioWebhook):
    """Twilio's call to a linked number. Druks answers with TwiML that streams the audio to
    the calls server."""

    category = "calls"

    def get_action(self) -> str:
        return "call"

    async def on_call(self) -> Response:
        """Start the caller's conversation for the call, and tell Twilio to stream its audio
        to the calls server. Twilio strips a query string from a stream URL, so the call
        token goes in the path."""
        session = db_session()
        connection = await Twilio.get_for_number(session, self.data["To"])
        if connection and await Voice.is_connected():
            caller = self.data["From"]
            # Twilio sends a word, such as "anonymous", in place of a hidden number. Each
            # such call is a new person, so hidden callers share no history.
            has_number = caller.startswith("+")
            conversation = await Conversation.get_or_create_for_user(
                session,
                connection,
                account_id=connection.account_id,
                source=ConversationSource.CALL,
                user_id=caller if has_number else self.data["CallSid"],
                user_name="",
                user_phone=caller if has_number else "",
                thread_id=self.data["CallSid"],
            )
            url = get_stream_url(get_call_token(conversation))
            twiml = f'<Response><Connect><Stream url="{url}"/></Connect><Hangup/></Response>'
            return Response(content=twiml, media_type="text/xml")
        return Response(content="<Response><Reject/></Response>", media_type="text/xml")


class CallsEvents(Webhook):
    """The calls server's requests for a call. Each one carries the call's token."""

    provider = "calls"
    category = "events"

    def get_action(self) -> str:
        return self.data["action"]

    async def request_is_authentic(self) -> bool:
        session = db_session()
        self.conversation = await get_call(session, self.data["token"])
        if self.conversation and self.get_action() == "pickup":
            # The calls server forwards the signature of Twilio's stream handshake.
            card = await Twilio.get()
            verify_twilio_signature(
                url=get_stream_url(self.data["token"]),
                fields={},
                signature=self.data["signature"],
                auth_token=card.secrets["auth_token"],
            )
        return bool(self.conversation)

    async def on_pickup(self) -> Response:
        """Give the calls server what the call needs, once: the prompt, the caller's facts,
        the Bot's key, the Voice card, and the timers."""
        session = db_session()
        conversation = self.conversation
        # The key holds no secret. It only marks the call as picked up.
        is_first_pickup = await get_client().set(
            name=f"calls:{conversation.id}:pickup", value="1", nx=True, ex=CALL_TOKEN_SECONDS
        )
        if not is_first_pickup:
            raise HTTPException(409, "The calls server picked up this call already.")
        _, prompt, tools = await get_agent(session, conversation)
        key = await get_druks_account_token(
            session, account_id=conversation.account_id, allowed_tools=tools, name=CALLS_KEY_NAME
        )
        voice = await Voice.get()
        local_time = datetime.now(tz=ZoneInfo(load_settings().timezone))
        earlier_lines = await self.list_earlier_lines(session)
        run_outcomes = await self.end_run_outcomes(session)
        return JSONResponse(
            {
                "prompt": f"{prompt}\n\n{CALL_PROMPT}",
                "facts": {
                    "caller": conversation.user_phone,
                    "number": conversation.connection.identity["number"],
                    "local_time": local_time.isoformat(),
                    "earlier_lines": earlier_lines,
                    "run_outcomes": [outcome.body for outcome in run_outcomes],
                },
                "mcp": {
                    "url": get_druks_mcp_server(allowed_tools=()).url,
                    "bearer": key.secrets["value"].removeprefix(BEARER_PREFIX),
                    "conversation": conversation.id,
                },
                "voice": {**voice.identity, **voice.secrets},
                "limits": {
                    "max_call_seconds": MAX_CALL_SECONDS,
                    "no_speech_seconds": NO_SPEECH_SECONDS,
                },
            }
        )

    @property
    def earlier_call_ids(self) -> Select:
        """The ids of the caller's other calls to this number."""
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
            .where(Message.conversation_id.in_(self.earlier_call_ids), ~Message.is_internal)
            .order_by(Conversation.created_at.desc(), Message.source_id.desc())
            .limit(EARLIER_LINES)
        )
        return [
            {
                "role": line.role,
                "text": line.transcript or line.body,
                "said_at": line.created_at.isoformat(),
            }
            for line in reversed(lines.all())
        ]

    async def end_run_outcomes(self, session: AsyncSession) -> list[Message]:
        """What Druks reported on the caller's earlier calls after they ended, oldest first.
        This call takes the reports, so they count as answered."""
        outcomes = await session.scalars(
            update(Message)
            .where(
                Message.conversation_id.in_(self.earlier_call_ids),
                Message.state == MessageState.PENDING,
            )
            .values(state=MessageState.REPLIED)
            .returning(Message)
        )
        return sorted(outcomes, key=attrgetter("created_at", "id"))

    async def on_line(self) -> Response:
        """Save a line of the call under its sequence. The calls server numbers the lines
        in the order they were said, so the call reads in that order when a line arrives
        late. A caller line waits for the assistant's next line."""
        session = db_session()
        conversation = self.conversation
        # Zero-padded, so a call's lines sort by their source ids.
        source_id = f"{conversation.thread_id}:{int(self.data['sequence']):08d}"
        if self.data["role"] == MessageRole.USER:
            line = await conversation.create_message(
                session, body="", transcript=self.data["text"], source_id=source_id
            )
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
                body=self.data["text"],
                role=MessageRole.ASSISTANT,
                reply_to=caller_line,
                source_id=source_id,
            )
            await conversation.end_turn(session, state=MessageState.REPLIED)
        return JSONResponse({"accepted": True})

    async def on_ended(self) -> Response:
        """Close a caller line that nobody answered."""
        await self.conversation.end_turn(db_session(), state=MessageState.INTERRUPTED)
        return JSONResponse({"accepted": True})
