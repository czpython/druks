from types import SimpleNamespace

import pytest
from conftest import bind_ambient_session, connect_service
from druks.accounts.enums import AccountKind
from druks.accounts.models import Account
from druks.agents import Bot
from druks.apps import App, loader
from druks.apps.registry import bots
from druks.chat import service
from druks.chat.bots import service as bot_service
from druks.chat.channels.calls.channel import CallsChannel
from druks.chat.models import Conversation
from druks.core.apis.twilio import TwilioClient
from druks.core.services import Twilio
from druks.notifications.services import validate_in_app_answer
from druks.secrets.models import VaultSecret
from druks.testing import seed_run
from sqlalchemy import func, select

CALLER = "+15550199"
VOICE_URL = "https://hooks.test/_external/twilio/calls/"


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
    """A fake Twilio account that holds one number, PN1."""
    calls = []
    settings = SimpleNamespace(urls=SimpleNamespace(webhook_base="https://hooks.test"))
    monkeypatch.setattr("druks.chat.channels.calls.routes.load_settings", lambda: settings)

    async def request(client, method, path, **values):
        calls.append((method, path, values.get("data")))
        return {"sid": "PN1", "phone_number": "+15550100"}

    monkeypatch.setattr(TwilioClient, "request", request)
    return calls


async def link(session):
    bind_ambient_session(session)
    await connect_service("twilio", identity={"account_sid": "AC1"}, secrets={"auth_token": "t"})
    owner = await Account.create_for_bot(session, AccountKind.BOT)
    connection = await Twilio.link(session, owner, app="helpdesk", sid="PN1", voice_url=VOICE_URL)
    await session.refresh(connection, ["account"])
    return connection


async def call(session, connection):
    return await Conversation.get_or_create_for_user(
        session,
        connection,
        connection.account_id,
        source=CallsChannel.name,
        user_id=CALLER,
        user_name="",
        user_phone=CALLER,
        thread_id="CA1",
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


async def test_a_run_outcome_on_a_call_starts_no_delivery(druks_db, twilio):
    conversation = await call(druks_db, await link(druks_db))
    run = await seed_run(druks_db, kind="test", state="failed")
    run.conversation_id = conversation.id

    assert not await service.report_failure(druks_db, run, failure="the sandbox died")

    [message] = await conversation.list_pending_messages(druks_db)
    assert message.is_internal


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
