import base64
import hashlib
import hmac
import re
import time
from operator import attrgetter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import bind_ambient_session, connect_service
from druks.accounts.enums import AccountKind
from druks.accounts.models import Account, PersonalAccessToken
from druks.agents import Bot
from druks.apps import App, loader
from druks.apps.registry import bots
from druks.chat import service
from druks.chat.bots import service as bot_service
from druks.chat.channels.calls import webhooks
from druks.chat.channels.calls.channel import CallsChannel
from druks.chat.enums import MessageState
from druks.chat.models import Conversation
from druks.core.apis.twilio import TwilioClient
from druks.core.services import Twilio
from druks.notifications.services import validate_in_app_answer
from druks.secrets.models import VaultSecret
from druks.testing import seed_run
from sqlalchemy import func, select

CALLER = "+15550199"
VOICE_URL = "https://hooks.test/_external/twilio/calls/"
VOICE_EVENTS = "/_external/voice/events/"


@pytest.fixture
def helpdesk(monkeypatch):
    declared = dict(bots._items)

    class Helpdesk(App):
        name = "helpdesk"
        bot = Bot(prompt="helpdesk/bot.md", user_tools=("get_ticket",))

    installed = loader.iter_apps()
    monkeypatch.setattr(loader, "iter_apps", lambda: [*installed, Helpdesk])
    yield Helpdesk
    bots._items.clear()
    bots._items.update(declared)


@pytest.fixture
def twilio(monkeypatch):
    """A fake Twilio account that holds one number, PN1, and Druks's public address."""
    calls = []
    settings = SimpleNamespace(
        urls=SimpleNamespace(webhook_base="https://hooks.test"), timezone="UTC"
    )
    for module in (
        "druks.chat.channels.calls.routes",
        "druks.chat.channels.calls.webhooks",
        "druks.core.webhooks.twilio",
        "druks.mcp.inbound",
    ):
        monkeypatch.setattr(f"{module}.load_settings", lambda: settings)

    async def request(client, method, path, **values):
        calls.append((method, path, values.get("data")))
        return {"sid": "PN1", "phone_number": "+15550100"}

    monkeypatch.setattr(TwilioClient, "request", request)
    return calls


@pytest.fixture
async def voice(druks_db, helpdesk, monkeypatch):
    """The Voice card and the Bot's prompt that a pickup reads."""
    bind_ambient_session(druks_db)
    await connect_service(
        "voice",
        identity={"model": "openai/gpt-realtime-mini", "voice_name": ""},
        secrets={"key": "sk-voice"},
    )
    monkeypatch.setattr(service, "render_prompt", AsyncMock(return_value="Help with tickets."))


def twilio_signature(url, fields):
    payload = url + "".join(f"{name}{fields[name]}" for name in sorted(fields))
    return base64.b64encode(hmac.new(b"t", payload.encode(), hashlib.sha1).digest()).decode()


async def link(session):
    bind_ambient_session(session)
    await connect_service("twilio", identity={"account_sid": "AC1"}, secrets={"auth_token": "t"})
    owner = await Account.create_for_bot(session, AccountKind.BOT)
    connection = await Twilio.link(session, owner, app="helpdesk", sid="PN1", voice_url=VOICE_URL)
    await session.refresh(connection, ["account"])
    return connection


async def call(session, connection, call_sid="CA1"):
    return await Conversation.get_or_create_for_user(
        session,
        connection,
        connection.account_id,
        source=CallsChannel.name,
        user_id=CALLER,
        user_name="",
        user_phone=CALLER,
        thread_id=call_sid,
    )


async def ring(client, **fields):
    signature = twilio_signature(VOICE_URL, fields)
    return await client.post(
        "/_external/twilio/calls/", data=fields, headers={"X-Twilio-Signature": signature}
    )


async def pick_up(client, token):
    signature = twilio_signature(webhooks.get_stream_url(token), {})
    return await client.post(
        VOICE_EVENTS, json={"action": "pickup", "token": token, "signature": signature}
    )


async def test_a_linked_number_sends_its_calls_to_druks_until_it_is_removed(
    druks_db, druks_client, helpdesk, twilio
):
    bind_ambient_session(druks_db)
    await connect_service("twilio", identity={"account_sid": "AC1"}, secrets={"auth_token": "t"})

    added = await druks_client.post(
        "/api/chat/services/twilio/numbers", json={"app": "helpdesk", "sid": "PN1"}
    )

    assert added.status_code == 201
    connection = await druks_db.get(VaultSecret, added.json()["id"])
    assert connection.account.kind == AccountKind.BOT
    assert connection.identity == {"app": "helpdesk", "number": "+15550100", "sid": "PN1"}
    assert connection.secrets["signing_secret"]
    assert not await druks_db.scalar(
        select(func.count()).select_from(Account).where(Account.kind == AccountKind.BOT_ADMIN)
    )
    voice_url = {"VoiceUrl": VOICE_URL, "VoiceMethod": "POST"}
    assert twilio[-1] == ("POST", "/IncomingPhoneNumbers/PN1.json", voice_url)
    again = await druks_client.post(
        "/api/chat/services/twilio/numbers", json={"app": "helpdesk", "sid": "PN1"}
    )
    assert again.status_code == 409

    removed = await druks_client.delete(f"/api/chat/services/twilio/numbers/{connection.id}")

    assert removed.status_code == 204
    no_url = {"VoiceUrl": "", "VoiceMethod": "POST"}
    assert twilio[-1] == ("POST", "/IncomingPhoneNumbers/PN1.json", no_url)
    await druks_db.refresh(connection)
    assert connection.revoked_at


async def test_disconnecting_twilio_removes_its_numbers_first(druks_db, druks_client, twilio):
    connection = await link(druks_db)

    response = await druks_client.delete("/api/services/twilio")

    assert response.status_code == 204
    no_url = {"VoiceUrl": "", "VoiceMethod": "POST"}
    assert twilio[-1] == ("POST", "/IncomingPhoneNumbers/PN1.json", no_url)
    await druks_db.refresh(connection)
    assert connection.revoked_reason == "service_disconnected"
    assert not await Twilio.is_connected()


async def test_a_call_to_a_linked_number_streams_to_the_voice_server(
    druks_db, druks_client, twilio, voice
):
    connection = await link(druks_db)
    linked_call = {"CallSid": "CA1", "From": CALLER, "To": "+15550100", "CallerCity": ""}
    other_call = {"CallSid": "CA2", "From": CALLER, "To": "+15550111"}

    unsigned_answer = await druks_client.post("/_external/twilio/calls/", data=linked_call)
    stream_answer = await ring(druks_client, **linked_call)
    other_answer = await ring(druks_client, **other_call)

    assert unsigned_answer.status_code == 401
    assert other_answer.text == "<Response><Reject/></Response>"
    [conversation] = await Conversation.list_for_connection(druks_db, connection.id)
    assert (conversation.account_id, conversation.source) == (connection.account_id, "call")
    assert (conversation.user_phone, conversation.thread_id) == (CALLER, "CA1")
    stream_url = re.search(r'<Stream url="([^"]+)"/>', stream_answer.text)[1]
    assert stream_url.startswith("wss://hooks.test/_voice/calls/")
    assert await webhooks.get_call(druks_db, stream_url.rpartition("/")[2]) == conversation


async def test_callers_who_hide_their_number_share_no_history(
    druks_db, druks_client, twilio, voice
):
    await link(druks_db)
    tokens = []
    for call_sid in ("CA1", "CA2"):
        answer = await ring(druks_client, CallSid=call_sid, From="anonymous", To="+15550100")
        tokens.append(re.search(r'/calls/([^"]+)"', answer.text)[1])
    line = {"action": "line", "sequence": 1, "role": "user", "text": "Is my refund done?"}
    await druks_client.post(VOICE_EVENTS, json={**line, "token": tokens[0]})

    pickup = (await pick_up(druks_client, tokens[1])).json()

    assert (pickup["facts"]["caller"], pickup["facts"]["earlier_lines"]) == ("", [])


async def test_a_pickup_hands_over_the_call_once(druks_db, druks_client, twilio, voice):
    connection = await link(druks_db)
    first_token = webhooks.get_call_token(await call(druks_db, connection, "CA1"))
    for sequence, role, text in [(1, "user", "Is my ticket open?"), (2, "assistant", "It is.")]:
        line = {"action": "line", "sequence": sequence, "role": role, "text": text}
        await druks_client.post(VOICE_EVENTS, json={**line, "token": first_token})
    second_call = await call(druks_db, connection, "CA2")
    token = webhooks.get_call_token(second_call)

    first_pickup = await pick_up(druks_client, token)
    second_pickup = await pick_up(druks_client, token)

    assert first_pickup.status_code == 200
    pickup = first_pickup.json()
    assert pickup["prompt"].startswith("Help with tickets.")
    lines = [(line["role"], line["text"]) for line in pickup["facts"]["earlier_lines"]]
    assert lines == [("user", "Is my ticket open?"), ("assistant", "It is.")]
    assert (pickup["facts"]["caller"], pickup["facts"]["number"]) == (CALLER, "+15550100")
    key = await PersonalAccessToken.authenticate(druks_db, pickup["mcp"]["bearer"])
    tools = ["helpdesk_get_ticket"]
    assert (key.account_id, key.allowed_tools) == (second_call.account_id, tools)
    assert pickup["mcp"]["conversation"] == second_call.id
    assert pickup["voice"] == {
        "model": "openai/gpt-realtime-mini",
        "voice_name": "",
        "key": "sk-voice",
    }
    assert second_pickup.status_code == 409


async def test_a_pickup_needs_a_valid_token_and_twilios_signature(druks_db, druks_client, twilio):
    connection = await link(druks_db)
    conversation = await call(druks_db, connection)
    token = webhooks.get_call_token(conversation)
    claim = f"{conversation.id}.{int(time.time()) - 1}"
    expired = f"{claim}.{webhooks.sign_claim(connection, claim)}"
    forged = f"{token.rpartition('.')[0]}.{webhooks.sign_claim(connection, 'another claim')}"
    unsigned_pickup = {"action": "pickup", "token": token, "signature": "not Twilio's"}

    answers = [
        await pick_up(druks_client, expired),
        await pick_up(druks_client, forged),
        await druks_client.post(VOICE_EVENTS, json=unsigned_pickup),
    ]

    assert [answer.status_code for answer in answers] == [401, 401, 401]


async def test_the_lines_of_a_call_land_in_the_order_they_were_said(druks_db, druks_client, twilio):
    conversation = await call(druks_db, await link(druks_db))
    token = webhooks.get_call_token(conversation)
    # The caller's second sentence reaches Druks before the first.
    lines = [
        (2, "user", "The one from Monday."),
        (1, "user", "Is my ticket open?"),
        (3, "assistant", "It is open."),
        (4, "user", "Thanks, and"),
    ]

    for sequence, role, text in lines:
        line = {"action": "line", "sequence": sequence, "role": role, "text": text}
        await druks_client.post(VOICE_EVENTS, json={**line, "token": token})
    await druks_client.post(VOICE_EVENTS, json={"action": "ended", "token": token})

    await druks_db.refresh(conversation, ["messages"])
    transcript = sorted(conversation.messages, key=attrgetter("source_id"))
    said = [(message.transcript or message.body, message.state) for message in transcript]
    assert said == [
        ("Is my ticket open?", MessageState.REPLIED),
        ("The one from Monday.", MessageState.REPLIED),
        ("It is open.", None),
        ("Thanks, and", MessageState.INTERRUPTED),
    ]
    assert transcript[2].reply_to == transcript[1].id


async def test_a_failed_run_from_a_call_waits_for_the_callers_next_call(
    druks_db, druks_client, twilio, voice
):
    connection = await link(druks_db)
    first_call = await call(druks_db, connection, "CA1")
    run = await seed_run(druks_db, kind="test", state="failed")
    run.conversation_id = first_call.id

    assert not await service.report_failure(druks_db, run, failure="The ticket is locked.")

    [outcome] = await first_call.list_pending_messages(druks_db)
    second_call = await call(druks_db, connection, "CA2")
    pickup = (await pick_up(druks_client, webhooks.get_call_token(second_call))).json()

    assert pickup["facts"]["run_outcomes"] == [outcome.body]
    await druks_db.refresh(outcome)
    assert outcome.state == MessageState.REPLIED


async def test_an_operator_answers_a_run_that_a_call_parked(druks_db, twilio):
    conversation = await call(druks_db, await link(druks_db))
    run = await seed_run(
        druks_db,
        kind="test",
        state="parked",
        input_gate="review",
        input_request={"presentation": "in_app", "controls": ["approve"], "questions": []},
    )
    run.conversation_id = conversation.id
    operator = await Account.get_or_create(druks_db, "op@example.com")

    answer = await validate_in_app_answer(
        druks_db, run, account_id=operator.id, control="approve", answers={}, note=""
    )

    assert answer["action"] == "approve"
    assert not await bot_service.ask_admin(druks_db, run)
