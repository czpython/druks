import hashlib
import hmac
import json
from types import SimpleNamespace
from typing import Annotated
from unittest.mock import AsyncMock

import httpx
import pytest
from conftest import bind_ambient_session, connect_service
from druks.accounts.dependencies import current_account, resolve_single_operator
from druks.accounts.enums import AccountKind
from druks.accounts.models import Account, PersonalAccessToken
from druks.agents import Bot, BotAccess, BotUser
from druks.api.dependencies import request_session
from druks.apps import App, loader
from druks.apps.exceptions import AppBotError
from druks.apps.registry import bots
from druks.chat import service
from druks.chat.app import Chat
from druks.chat.bots import routes as bot_routes
from druks.chat.bots import service as bot_service
from druks.chat.bots.constants import PAUSE_TOPIC, PHONE_CONNECTED_MESSAGE
from druks.chat.bridge import Bridge
from druks.chat.channels.whatsapp import routes
from druks.chat.channels.whatsapp.client import WahaClient
from druks.chat.channels.whatsapp.constants import WAHA_AUDIENCE
from druks.chat.channels.whatsapp.services import Waha
from druks.chat.channels.whatsapp.webhooks import WahaEvents
from druks.chat.constants import CONVERSATION_HEADER
from druks.chat.enums import ConversationSource, MessageRole, MessageState, PauseSignal
from druks.chat.models import Conversation
from druks.harnesses.claude import ClaudeHarness
from druks.mcp.enums import Toolkit
from druks.mcp.server import _is_visible, _validate_agent_tools
from druks.models import Base
from druks.notifications.exceptions import AnswerNotAllowedError
from druks.notifications.services import validate_in_app_answer
from druks.secrets.enums import SecretKind
from druks.secrets.models import VaultSecret
from druks.settings import Urls
from druks.testing import asgi_client, configure_app_for_test, make_settings, seed_run
from druks.user_settings import reads
from druks.user_settings.models import InstallationSettings
from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException
from sqlalchemy import func, select

ANA = "41700000001@c.us"
BEN = "41700000002@c.us"
NUMBER = "41000000000@c.us"


@pytest.fixture
def helpdesk(monkeypatch, request):
    declared = dict(bots._items)

    class Helpdesk(App):
        name = "helpdesk"
        bot = Bot(
            prompt="helpdesk/bot.md",
            access=getattr(request, "param", BotAccess.OPEN),
            user_tools=("get_ticket",),
            admin_tools=("list_requests",),
        )

    installed = loader.iter_apps()
    monkeypatch.setattr(loader, "iter_apps", lambda: [*installed, Helpdesk])
    yield Helpdesk
    bots._items.clear()
    bots._items.update(declared)


@pytest.fixture
def waha(monkeypatch):
    calls = []
    answers = {
        ("POST", "/api/sessions"): {"name": "session_two"},
        ("POST", "/api/keys"): {"id": "key_id_two", "key": "key_two"},
        ("GET", "/api/session_one/new-message-id"): {"id": "REPLY1"},
    }
    handlers = {}

    async def request(client, method, path, *, accept="*/*", **values):
        calls.append((method, path, values.get("json")))
        if handler := handlers.get((method, path)):
            await handler()
        return httpx.Response(200, json=answers.get((method, path), {}))

    monkeypatch.setattr(WahaClient, "request", request)
    return SimpleNamespace(calls=calls, handlers=handlers)


async def link(
    session,
    owner,
    *,
    app="helpdesk",
    access=BotAccess.OPEN,
    session_name="session_one",
    number="+41000000000",
):
    bind_ambient_session(session)
    await connect_service("waha", identity={"url": "http://waha.test"}, secrets={"key": "admin"})
    identity = {}
    if app:
        if access == BotAccess.PAIRED:
            identity = {"app": app, "operators": {}}
        else:
            admin = await Account.create_for_bot(session, AccountKind.BOT_ADMIN)
            identity = {"app": app, "admin": {"account_id": admin.id}}
    if number:
        identity = {**identity, "number": number, "user_id": NUMBER}
    identity = {**identity, "session": session_name}
    connection = await VaultSecret.store(
        session,
        SecretKind.SESSION,
        WAHA_AUDIENCE,
        secrets={
            "key": "key_one",
            "key_id": "key_id_one",
            "webhook_secret": "hook-secret",
        },
        identity=identity,
        account_id=owner.id,
    )
    await session.refresh(connection, ["account"])
    return connection


def user(chat, name=""):
    return {
        "source": ConversationSource.WHATSAPP,
        "user_id": chat,
        "user_name": name,
        "user_phone": "",
    }


async def receive(connection, event):
    webhook = WahaEvents(None, {}, None)
    webhook.raw_body = json.dumps(event).encode()
    webhook.connection = connection
    await webhook.on_message_any()


def message_event(chat, body, *, key="MSG1", from_me=False, session_name="session_one"):
    return {
        "id": f"evt_{key}",
        "event": "message.any",
        "session": session_name,
        "me": {"id": NUMBER, "pushName": "Helpdesk", "lid": "900@lid"},
        "payload": {
            "id": f"{str(from_me).lower()}_{chat}_{key}",
            "from": chat,
            "fromMe": from_me,
            "body": body,
            "hasMedia": False,
            "_data": {"pushName": "Ana"},
        },
    }


def status_event(me_id=NUMBER, session_name="session_one", status="WORKING", engine="GOWS"):
    return {
        "id": "evt_status",
        "event": "session.status",
        "session": session_name,
        "engine": engine,
        "me": {"id": me_id, "pushName": "Helpdesk"},
        "payload": {"name": session_name, "status": status},
    }


async def bot_account(session):
    return await Account.create_for_bot(session, AccountKind.BOT)


def webhook_base(monkeypatch):
    settings = SimpleNamespace(urls=SimpleNamespace(webhook_base="https://hooks.test"))
    monkeypatch.setattr("druks.chat.channels.whatsapp.services.load_settings", lambda: settings)


async def test_the_webhook_accepts_only_an_event_its_session_signed(druks_db, druks_client):
    connection = await link(druks_db, await bot_account(druks_db), number="")
    body = json.dumps(status_event()).encode()
    signature = hmac.new(b"hook-secret", body, hashlib.sha512).hexdigest()

    missing = await druks_client.post("/_external/waha/events/", content=body)
    wrong = await druks_client.post(
        "/_external/waha/events/", content=body, headers={"X-Webhook-Hmac": "0" * 128}
    )
    signed = await druks_client.post(
        "/_external/waha/events/", content=body, headers={"X-Webhook-Hmac": signature}
    )

    assert [missing.status_code, wrong.status_code, signed.status_code] == [401, 401, 200]
    await druks_db.refresh(connection)
    facts = {key: connection.identity[key] for key in ("number", "name", "user_id")}
    assert facts == {"number": "+41000000000", "name": "Helpdesk", "user_id": NUMBER}
    assert connection.identity_status == "resolved"


async def test_adding_a_number_creates_its_accounts_and_session(
    druks_db, druks_client, helpdesk, waha, monkeypatch
):
    webhook_base(monkeypatch)
    await connect_service("waha", identity={"url": "http://waha.test"}, secrets={"key": "admin"})

    response = await druks_client.post("/api/chat/services/waha/sessions", json={"app": "helpdesk"})

    assert response.status_code == 201
    connection = await druks_db.get(VaultSecret, response.json()["id"])
    assert connection.account.kind == AccountKind.BOT
    admin = await druks_db.get(Account, connection.identity["admin"]["account_id"])
    assert admin.kind == AccountKind.BOT_ADMIN
    assert connection.identity == {
        "app": "helpdesk",
        "admin": {"account_id": admin.id},
        "session": "session_two",
    }
    assert [(method, path) for method, path, _ in waha.calls] == [
        ("POST", "/api/sessions"),
        ("POST", "/api/keys"),
        ("PUT", "/api/sessions/session_two"),
    ]
    config = waha.calls[-1][2]["config"]
    assert config["webhooks"] == [
        {
            "url": "https://hooks.test/_external/waha/events/",
            "events": ["message.any", "session.status"],
            "hmac": {"key": connection.secrets["webhook_secret"]},
        }
    ]
    assert config["ignore"] == {"status": True, "groups": True, "channels": True, "broadcast": True}
    assert config["noweb"] == {"markOnline": False}


async def test_a_number_cannot_be_linked_twice_or_on_an_engine_druks_cannot_read(druks_db, waha):
    await link(druks_db, await bot_account(druks_db))
    second = await link(druks_db, await bot_account(druks_db), session_name="session_2", number="")
    third = await link(druks_db, await bot_account(druks_db), session_name="session_3", number="")

    await Waha.update_status(druks_db, second, status_event(session_name="session_2"))
    await Waha.update_status(
        druks_db, third, status_event("41000000009@c.us", "session_3", engine="WEBJS")
    )

    assert second.revoked_at
    assert second.revoked_reason == "number_already_linked"
    assert ("DELETE", "/api/sessions/session_2", None) in waha.calls
    assert third.revoked_reason == "unsupported_engine"


async def test_chat_links_a_number_without_an_admin_account(
    druks_db, druks_client, waha, monkeypatch
):
    webhook_base(monkeypatch)
    await connect_service("waha", identity={"url": "http://waha.test"}, secrets={"key": "admin"})

    response = await druks_client.post("/api/chat/services/waha/sessions", json={"app": "chat"})

    assert response.status_code == 201
    connection = await druks_db.get(VaultSecret, response.json()["id"])
    assert connection.account.kind == AccountKind.BOT
    assert connection.identity == {"app": "chat", "operators": {}, "session": "session_two"}
    assert not response.json()["isPhoneConnected"]
    assert not await druks_db.scalar(
        select(func.count()).select_from(Account).where(Account.kind == AccountKind.BOT_ADMIN)
    )
    settings = await druks_client.get("/api/settings/apps")
    [chat] = [app for app in settings.json()["apps"] if app["name"] == "chat"]
    assert chat["builtin"]
    assert chat["bot"] == "chat.bot"
    assert chat["botAccess"] == "paired"
    assert [agent["name"] for agent in chat["agents"]] == ["chat.bot"]


async def test_a_session_that_stops_working_is_no_longer_linked(druks_db):
    connection = await link(druks_db, await bot_account(druks_db))

    await Waha.update_status(druks_db, connection, status_event())
    await Waha.update_status(druks_db, connection, status_event(status="FAILED"))

    assert connection.identity_status == "unavailable"
    assert connection.identity["number"] == "+41000000000"


@pytest.mark.parametrize("sender, prefix", [(NUMBER, "[Druks] "), (ANA, "")])
async def test_only_self_chat_replies_have_a_prefix(druks_db, waha, sender, prefix):
    owner = await bot_account(druks_db)
    connection = await link(druks_db, owner)
    conversation = await Conversation.get_or_create_for_user(
        druks_db, connection, owner.id, **user(sender)
    )
    reply = await conversation.create_message(
        druks_db, "Your ticket is open.", role=MessageRole.ASSISTANT
    )

    await Waha.send_reply(druks_db, conversation, reply)

    assert waha.calls[-1][2]["text"] == f"{prefix}Your ticket is open."
    await druks_db.refresh(reply)
    assert reply.body == "Your ticket is open."


async def test_a_message_reaches_the_bot_of_its_numbers_app(druks_db, helpdesk, monkeypatch):
    owner = await bot_account(druks_db)
    connection = await link(druks_db, owner)

    await receive(connection, message_event(ANA, "Is my ticket open?"))

    [conversation] = await Conversation.list_for_connection(druks_db, connection.id)
    assert conversation.account_id == owner.id
    assert (conversation.user_id, conversation.user_phone) == (ANA, "+41700000001")
    get_config = AsyncMock(return_value="config")
    monkeypatch.setattr(service, "get_config", get_config)
    monkeypatch.setattr(service, "render_prompt", AsyncMock(return_value="Be kind."))

    assert await service.get_agent(druks_db, conversation) == (
        "config",
        "Be kind.",
        ("helpdesk_get_ticket",),
    )
    assert get_config.await_args.args[1] == "helpdesk.bot"


async def test_one_turn_answers_every_pending_message_and_knows_its_own_reply(
    druks_db, helpdesk, waha, monkeypatch
):
    connection = await link(druks_db, await bot_account(druks_db))
    for key, body in (("M1", "Hello"), ("M2", "Is my ticket open?"), ("M3", "And the second one?")):
        await receive(connection, message_event(ANA, body, key=key))
    [conversation] = await Conversation.list_for_connection(druks_db, connection.id)
    config = SimpleNamespace(
        harness_class=ClaudeHarness, model_id="m", effort="", fast_mode=False, timeout=60
    )
    tools = ("helpdesk_get_ticket",)
    monkeypatch.setattr(service, "get_agent", AsyncMock(return_value=(config, "Be kind.", tools)))
    monkeypatch.setattr(
        "druks.mcp.inbound.load_settings",
        lambda: SimpleNamespace(urls=Urls(webhook_host="hooks.test", endpoint="")),
    )
    host = SimpleNamespace(id="bot-sandbox", ssh_username="druks", aclose=AsyncMock())
    monkeypatch.setattr(service, "get_sandbox", AsyncMock(return_value=(host, SimpleNamespace())))
    monkeypatch.setattr(service, "sandbox_client", SimpleNamespace(set_expiry=AsyncMock()))
    monkeypatch.setattr(Bridge, "reply", AsyncMock(return_value=("Your ticket is open.", [])))
    state = {"status": "missing", "sessionId": "", "messageId": ""}
    requests = []

    async def request(self, method, **values):
        requests.append((method, values))
        if method == "prompt":
            state.update(
                status="replied",
                messageId=values["messageId"],
                epoch="one",
                sequence=0,
                archivePath="",
            )
        return {"events": []} if method == "events" else state

    async def copy_arrives_first() -> None:
        # WAHA can report the sent message before the send returns.
        echo = message_event(ANA, "Your ticket is open.", key="REPLY1", from_me=True)
        await receive(connection, echo)

    monkeypatch.setattr(Bridge, "request", request)
    waha.handlers[("POST", "/api/sendText")] = copy_arrives_first
    pause = AsyncMock()
    monkeypatch.setattr(bot_service.pause_queue, "enqueue_async", pause)

    await service.deliver_pending(druks_db, conversation)

    [prompt] = [values for method, values in requests if method == "prompt"]
    assert prompt["body"] == "Hello\n\nIs my ticket open?\n\nAnd the second one?"
    assert prompt["timeout"] == 60
    [start] = [values for method, values in requests if method == "start"]
    assert start["headers"] == [{"name": CONVERSATION_HEADER, "value": conversation.id}]
    assert start["meta"]["claudeCode"]["options"]["tools"] == []
    assert start["meta"]["claudeCode"]["options"]["systemPrompt"] == "Be kind."
    await druks_db.refresh(conversation, ["messages"])
    *asked, reply = conversation.messages
    assert [message.state for message in asked] == [MessageState.REPLIED] * 3
    assert reply.source_id == "REPLY1"
    sends = [body for method, path, body in waha.calls if path == "/api/sendText"]
    assert sends == [
        {"session": "session_one", "chatId": ANA, "text": "Your ticket is open.", "id": "REPLY1"}
    ]
    pause.assert_not_awaited()


async def get_ticket(ticket: Annotated[str, Body(embed=True)], user: BotUser) -> dict:
    """Get a ticket for the person writing."""
    return {"user": user.id, "ticket": ticket}


def test_the_person_writing_never_enters_a_tool_schema():
    api = FastAPI()
    api.add_api_route("/tickets", get_ticket, methods=["POST"], tags=["bot"])

    operation = api.openapi()["paths"]["/tickets"]["post"]

    assert "parameters" not in operation
    schema = operation["requestBody"]["content"]["application/json"]["schema"]
    body = api.openapi()["components"]["schemas"][schema["$ref"].rpartition("/")[2]]
    assert body["properties"] == {"ticket": {"type": "string", "title": "Ticket"}}


async def test_a_bot_tool_serves_only_the_callers_own_conversation(druks_db, tmp_path):
    configure_app_for_test(settings=make_settings(tmp_path), authenticated=False)
    api = FastAPI(dependencies=[Depends(request_session)])
    api.add_api_route(
        "/tickets", get_ticket, methods=["POST"], dependencies=[Depends(current_account)]
    )
    owner = await bot_account(druks_db)
    connection = await link(druks_db, owner)
    conversation = await Conversation.get_or_create_for_user(
        druks_db, connection, owner.id, **user(ANA, "Ana")
    )
    operator = await Account.get_or_create(druks_db, "op@example.com")
    foreign = await Conversation.create(druks_db, account_id=operator.id, body="Hello")
    _, token = await PersonalAccessToken.create(druks_db, account_id=owner.id, name="bot")
    headers = {"Authorization": f"Bearer {token}"}

    async with asgi_client(api) as client:
        outside = await client.post("/tickets", json={"ticket": "T-1"}, headers=headers)
        other = await client.post(
            "/tickets",
            json={"ticket": "T-1"},
            headers={**headers, CONVERSATION_HEADER: foreign.id},
        )
        own = await client.post(
            "/tickets",
            json={"ticket": "T-1"},
            headers={**headers, CONVERSATION_HEADER: conversation.id},
        )

    assert [outside.status_code, other.status_code] == [409, 403]
    assert own.json() == {"user": ANA, "ticket": "T-1"}


def test_a_bot_tool_is_never_in_the_operator_toolkit():
    def visible(allowed_tools, name, tags):
        token = SimpleNamespace(claims={"allowed_tools": allowed_tools})
        return _is_visible(
            SimpleNamespace(token=token, component=SimpleNamespace(name=name, tags=tags))
        )

    assert not visible(None, "helpdesk_get_ticket", {"bot", "helpdesk"})
    assert visible(None, "get_usage", {"agent"})
    assert visible(["helpdesk_get_ticket"], "helpdesk_get_ticket", {"bot", "helpdesk"})
    assert not visible(["helpdesk_get_ticket"], "get_usage", {"agent"})
    assert not visible([], "get_usage", {"agent"})


def test_a_bot_under_another_name_or_an_unknown_tool_stops_boot(helpdesk):
    with pytest.raises(Exception) as declared:

        class Misnamed(App):
            name = "misnamed"
            front_desk = Bot(prompt="misnamed/desk.md")

    # Python 3.11 wraps an error in __set_name__; later versions raise it directly.
    assert isinstance(declared.value.__cause__ or declared.value, AppBotError)
    api = FastAPI()
    router = APIRouter()
    router.add_api_route(
        "/tickets/find", get_ticket, methods=["POST"], tags=["bot"], operation_id="find_tickets"
    )
    api.include_router(router, tags=["helpdesk"])
    with pytest.raises(AppBotError, match="get_ticket, list_requests"):
        _validate_agent_tools(api)


async def test_the_bot_row_saves_and_resolves_like_an_agent_row(druks_db, druks_client, helpdesk):
    response = await druks_client.patch(
        "/api/settings/apps",
        json={
            "agentEfforts": {"helpdesk.bot": "low"},
            "agentTimeouts": {"helpdesk.bot": 120},
        },
    )

    assert response.status_code == 200
    settings = await InstallationSettings.get_or_create(druks_db)
    row = await reads.get_agent_setting(druks_db, helpdesk.bot, settings=settings)
    assert (row.effort, row.effort_source) == ("low", "agent")
    assert (row.timeout, row.timeout_source) == (120, "agent")


async def test_the_admin_code_makes_its_sender_the_number_admin(druks_db, helpdesk):
    owner = await bot_account(druks_db)
    connection = await link(druks_db, owner)
    operator = await Account.get_or_create(druks_db, "op@example.com")
    code = await bot_service.open_admin_code(connection, operator.id)

    await receive(connection, message_event(ANA, f" {code} ", key="M1"))
    await receive(connection, message_event(ANA, "Requests today?", key="M2"))
    await receive(connection, message_event(BEN, code, key="M3"))

    admin = connection.identity["admin"]
    assert admin["user_id"] == ANA
    assert (await druks_db.get(Account, admin["account_id"])).kind == AccountKind.BOT_ADMIN
    conversations = {
        conversation.account_id: conversation
        for conversation in await Conversation.list_for_connection(druks_db, connection.id)
    }
    admin_chat, user_chat = conversations[admin["account_id"]], conversations[owner.id]
    await druks_db.refresh(admin_chat, ["messages"])
    await druks_db.refresh(user_chat, ["messages"])
    assert [(message.is_internal, message.body) for message in admin_chat.messages][1:] == [
        (False, "Requests today?")
    ]
    assert admin_chat.messages[0].is_internal
    assert (user_chat.user_id, [message.body for message in user_chat.messages]) == (BEN, [code])


async def test_bot_and_bot_admin_accounts_never_count_as_operators(druks_db):
    operator = await Account.get_or_create(druks_db, "op@example.com")
    await bot_account(druks_db)
    await Account.create_for_bot(druks_db, AccountKind.BOT_ADMIN)

    assert await resolve_single_operator(druks_db) == operator
    assert await Account.list_operators(druks_db) == [operator]


async def test_two_operators_pair_to_one_number_under_their_own_accounts(
    druks_db, tmp_path, monkeypatch
):
    connection = await link(
        druks_db, await bot_account(druks_db), app="chat", access=BotAccess.PAIRED
    )
    operators = [
        await Account.get_or_create(druks_db, "ana@example.com"),
        await Account.get_or_create(druks_db, "ben@example.com"),
    ]
    api = configure_app_for_test(
        settings=make_settings(tmp_path, identity={"mode": "header", "header": "X-User"}),
        authenticated=False,
    )
    delivery = AsyncMock()
    monkeypatch.setattr(bot_service.DBOS, "start_workflow_async", delivery)
    async with asgi_client(api) as client:
        for operator, sender in zip(operators, (ANA, BEN), strict=True):
            headers = {"X-User": operator.username}
            before = await client.get("/api/chat/services/waha/sessions?app=chat", headers=headers)
            assert not before.json()[0]["isPhoneConnected"]
            response = await client.post(
                f"/api/chat/connections/{connection.id}/admin-code",
                json={"account_id": connection.account_id},
                headers=headers,
            )
            assert response.status_code == 200
            code = response.json()["code"]
            await receive(connection, message_event(sender, code, key=f"proof-{operator.id}"))
            after = await client.get("/api/chat/services/waha/sessions?app=chat", headers=headers)
            assert after.json()[0]["isPhoneConnected"]
            conversations = await Conversation.list_for_connection(druks_db, connection.id)
            [conversation] = [chat for chat in conversations if chat.account_id == operator.id]
            await druks_db.refresh(conversation, ["messages"])
            assert [(message.body, message.is_internal) for message in conversation.messages] == [
                (PHONE_CONNECTED_MESSAGE, True)
            ]
            delivery.assert_awaited_with(service.deliver, conversation.id)
        assert delivery.await_count == 2
        await receive(connection, message_event("41700000003@c.us", code, key="used-proof"))
        assert delivery.await_count == 2

    assert connection.identity["operators"] == {ANA: operators[0].id, BEN: operators[1].id}
    for sender in (ANA, BEN):
        await receive(connection, message_event(sender, "Read my runs", key=sender))
    conversations = await Conversation.list_for_connection(druks_db, connection.id)
    assert {conversation.user_id: conversation.account_id for conversation in conversations} == {
        ANA: operators[0].id,
        BEN: operators[1].id,
    }
    for conversation in conversations:
        await druks_db.refresh(conversation, ["messages"])
        assert [message.body for message in conversation.messages] == [
            PHONE_CONNECTED_MESSAGE,
            "Read my runs",
        ]


async def test_disconnect_removes_only_the_signed_in_operators_phones(
    druks_db, tmp_path, monkeypatch
):
    connection = await link(
        druks_db, await bot_account(druks_db), app="chat", access=BotAccess.PAIRED
    )
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    ben = await Account.get_or_create(druks_db, "ben@example.com")
    second_phone = "41700000003@c.us"
    connection.identity = {
        **connection.identity,
        "operators": {ANA: ana.id, second_phone: ana.id, BEN: ben.id},
    }
    await druks_db.commit()
    api = configure_app_for_test(
        settings=make_settings(tmp_path, identity={"mode": "header", "header": "X-User"}),
        authenticated=False,
    )
    delivery = AsyncMock()
    monkeypatch.setattr(bot_service.DBOS, "start_workflow_async", delivery)

    async with asgi_client(api) as client:
        response = await client.delete(
            f"/api/chat/connections/{connection.id}/phone", headers={"X-User": ana.username}
        )
        assert response.status_code == 204
        for operator, is_connected in ((ana, False), (ben, True)):
            response = await client.get(
                "/api/chat/services/waha/sessions?app=chat", headers={"X-User": operator.username}
            )
            assert response.json()[0]["isPhoneConnected"] == is_connected
    await druks_db.refresh(connection)
    assert connection.identity["operators"] == {BEN: ben.id}
    for sender in (ANA, second_phone):
        await receive(connection, message_event(sender, "Read my runs", key=sender))
    delivery.assert_not_awaited()
    assert not await Conversation.list_for_connection(druks_db, connection.id)
    await receive(connection, message_event(BEN, "Read my runs", key=BEN))
    [conversation] = await Conversation.list_for_connection(druks_db, connection.id)
    assert conversation.account_id == ben.id


async def test_disconnect_refuses_an_open_number(druks_db, druks_client, helpdesk):
    connection = await link(druks_db, await bot_account(druks_db))
    identity = dict(connection.identity)

    response = await druks_client.delete(f"/api/chat/connections/{connection.id}/phone")

    assert response.status_code == 404
    await druks_db.refresh(connection)
    assert connection.identity == identity


async def test_a_paired_number_ignores_strangers_and_its_own_phone(druks_db, monkeypatch):
    connection = await link(
        druks_db, await bot_account(druks_db), app="chat", access=BotAccess.PAIRED
    )
    operator = await Account.get_or_create(druks_db, "op@example.com")
    code = await bot_service.open_admin_code(connection, operator.id)
    save_media, delivery, take_over = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(WahaEvents, "save_media", save_media)
    monkeypatch.setattr(bot_service.DBOS, "start_workflow_async", delivery)
    monkeypatch.setattr(bot_service, "take_over", take_over)

    for sender, is_from_phone in ((ANA, False), (ANA, True), (NUMBER, True), ("900@lid", True)):
        body = code if is_from_phone else "Hello"
        event = message_event(sender, body, key=f"{sender}-{is_from_phone}", from_me=is_from_phone)
        event["payload"].update(hasMedia=True, media={"url": "http://waha.test/media/one"})
        await receive(connection, event)

    assert not await Conversation.list_for_connection(druks_db, connection.id)
    assert not connection.identity["operators"]
    assert await bot_service.close_admin_code(connection, code) == operator.id
    save_media.assert_not_awaited()
    delivery.assert_not_awaited()
    take_over.assert_not_awaited()


@pytest.mark.parametrize(
    "source, app",
    [
        (ConversationSource.WEB, "chat"),
        (ConversationSource.WHATSAPP, ""),
        (ConversationSource.WHATSAPP, "chat"),
        (ConversationSource.WHATSAPP, "helpdesk"),
    ],
)
@pytest.mark.parametrize("helpdesk", [BotAccess.PAIRED], indirect=True)
async def test_operator_turns_use_the_apps_prompt_and_settings_without_a_timeout(
    druks_db, monkeypatch, helpdesk, source, app
):
    operator = await Account.get_or_create(druks_db, "op@example.com")
    if source == ConversationSource.WHATSAPP:
        connection = await link(
            druks_db,
            await bot_account(druks_db) if app else operator,
            app=app,
            access=BotAccess.PAIRED,
        )
        conversation = await Conversation.get_or_create_for_user(
            druks_db, connection, operator.id, **user(ANA)
        )
        await conversation.create_message(druks_db, "Read my runs")
    else:
        conversation = await Conversation.create(
            druks_db, account_id=operator.id, body="Read my runs"
        )
        await druks_db.refresh(conversation, ["account"])
    config = SimpleNamespace(
        harness_class=ClaudeHarness, model_id="m", effort="low", fast_mode=False, timeout=30
    )
    get_config = AsyncMock(return_value=config)
    render_prompt = AsyncMock(return_value="Operator prompt")
    monkeypatch.setattr(service, "get_config", get_config)
    monkeypatch.setattr(service, "render_prompt", render_prompt)
    monkeypatch.setattr(
        "druks.mcp.inbound.load_settings",
        lambda: SimpleNamespace(urls=Urls(webhook_host="hooks.test", endpoint="")),
    )
    monkeypatch.setattr(service, "sandbox_client", SimpleNamespace(set_expiry=AsyncMock()))
    request = AsyncMock(return_value={"status": "idle", "sessionId": "one"})
    monkeypatch.setattr(Bridge, "request", request)
    host = SimpleNamespace(id="operator-sandbox")
    message = await conversation.get_unanswered_message(druks_db)

    resolved_config, prompt, tools = await service.get_agent(druks_db, conversation)
    await service.send_turn(
        druks_db, conversation, message, Bridge(host), SimpleNamespace(), resolved_config, prompt
    )

    bot = helpdesk.bot if app == "helpdesk" else Chat.bot
    get_config.assert_awaited_once_with(druks_db, bot.id, operator.id)
    render_prompt.assert_awaited_once_with(bot.prompt, source=source)
    assert tools == Toolkit.ALL
    [start] = [call.kwargs for call in request.await_args_list if call.args[0] == "start"]
    assert start["meta"]["claudeCode"]["options"]["systemPrompt"] == {
        "type": "preset",
        "preset": "claude_code",
        "append": prompt,
    }
    [turn] = [call.kwargs for call in request.await_args_list if call.args[0] == "prompt"]
    assert turn["timeout"] == 0


async def test_an_operator_on_a_paired_number_keeps_normal_gate_access(druks_db):
    connection = await link(
        druks_db, await bot_account(druks_db), app="chat", access=BotAccess.PAIRED
    )
    operator = await Account.get_or_create(druks_db, "op@example.com")
    conversation = await Conversation.get_or_create_for_user(
        druks_db, connection, operator.id, **user(ANA)
    )
    run = await seed_run(
        druks_db,
        kind="test",
        state="parked",
        input_gate="review",
        input_request={"presentation": "in_app", "controls": ["approve"], "questions": []},
    )
    run.conversation_id = conversation.id

    assert not conversation.admin_account_id
    for account_id in (operator.id, None):
        answer = await validate_in_app_answer(
            druks_db, run, account_id=account_id, control="approve", answers={}, note=""
        )
        assert answer["action"] == "approve"
    assert not await bot_service.ask_admin(druks_db, run)
    await druks_db.refresh(conversation, ["messages"])
    assert not conversation.messages


async def test_only_the_number_admin_answers_its_questions_or_resumes_its_chats(
    druks_db, helpdesk, monkeypatch
):
    owner = await bot_account(druks_db)
    connection = await link(druks_db, owner)
    admin = await bot_service.add_admin(druks_db, connection, user(ANA, "Ana"))
    conversation = await Conversation.get_or_create_for_user(
        druks_db, connection, owner.id, **user(BEN, "Ben")
    )
    operator = await Account.get_or_create(druks_db, "op@example.com")
    run = await seed_run(
        druks_db,
        kind="test",
        state="parked",
        input_gate="review",
        input_request={"presentation": "in_app", "controls": ["approve"], "questions": []},
    )
    run.conversation_id = conversation.id

    for account_id in (operator.id, None):
        with pytest.raises(AnswerNotAllowedError):
            await validate_in_app_answer(
                druks_db, run, account_id=account_id, control="approve", answers={}, note=""
            )
    answer = await validate_in_app_answer(
        druks_db, run, account_id=admin.account_id, control="approve", answers={}, note=""
    )
    assert answer["action"] == "approve"
    with pytest.raises(HTTPException):
        await bot_routes.resume_conversation(druks_db, conversation.id, account=operator)
    resumed = await bot_routes.resume_conversation(
        druks_db, conversation.id, account=await druks_db.get(Account, admin.account_id)
    )
    assert resumed.result == "not_paused"


async def test_a_phone_message_pauses_its_chat_and_the_next_one_restarts_the_clock(
    druks_db, helpdesk, monkeypatch
):
    connection = await link(druks_db, await bot_account(druks_db))
    enqueue, send = AsyncMock(), AsyncMock()
    monkeypatch.setattr(bot_service.pause_queue, "enqueue_async", enqueue)
    monkeypatch.setattr(bot_service.DBOS, "send_async", send)
    typed = message_event(ANA, "I'll call you.", key="PHONE1", from_me=True)

    await receive(connection, typed)
    await receive(connection, typed)
    monkeypatch.setattr(Conversation, "get_pause_id", AsyncMock(return_value="PHONE1"))
    await receive(connection, message_event(ANA, "At noon.", key="PHONE2", from_me=True))

    [conversation] = await Conversation.list_for_connection(druks_db, connection.id)
    await druks_db.refresh(conversation, ["messages"])
    assert [message.is_internal for message in conversation.messages] == [True, True]
    assert "I'll call you." in conversation.messages[0].body
    enqueue.assert_awaited_once_with(bot_service.pause, conversation.id)
    send.assert_awaited_once_with("PHONE1", PauseSignal.EXTEND, topic=PAUSE_TOPIC)


async def test_the_phones_chat_with_itself_is_an_admin_chat(druks_db, helpdesk):
    connection = await link(druks_db, await bot_account(druks_db))

    await receive(connection, message_event(NUMBER, "Requests today?", key="N1", from_me=True))
    await receive(connection, message_event("900@lid", "And tomorrow?", key="N2", from_me=True))

    [chat] = await Conversation.list_for_connection(druks_db, connection.id)
    await druks_db.refresh(chat, ["messages"])
    assert (chat.account_id, chat.user_id) == (connection.identity["admin"]["account_id"], NUMBER)
    assert [message.body for message in chat.messages] == ["Requests today?", "And tomorrow?"]


async def test_a_personal_number_reaches_its_owner_only_from_the_self_chat(druks_db):
    operator = await Account.get_or_create(druks_db, "op@example.com")
    connection = await link(druks_db, operator, app="")

    await receive(connection, message_event(ANA, "Hi", key="M1"))
    await receive(connection, message_event(ANA, "On my way", key="M2", from_me=True))
    await receive(connection, message_event(NUMBER, "Remind me at six", key="M3", from_me=True))

    [conversation] = await Conversation.list_for_connection(druks_db, connection.id)
    await druks_db.refresh(conversation, ["messages"])
    assert (conversation.account_id, conversation.user_id) == (operator.id, NUMBER)
    assert [message.body for message in conversation.messages] == ["Remind me at six"]


async def test_removing_a_number_holds_its_chats_and_relinking_keeps_its_history(
    druks_db, druks_client, helpdesk, waha, monkeypatch
):
    webhook_base(monkeypatch)
    owner = await bot_account(druks_db)
    first = await link(druks_db, owner)
    await receive(first, message_event(ANA, "Hello", key="M1"))
    [chat] = await Conversation.list_for_connection(druks_db, first.id)
    resume = AsyncMock()
    monkeypatch.setattr(routes, "resume", resume)

    response = await druks_client.delete(f"/api/chat/services/waha/sessions/{first.id}")
    identity = {"app": "helpdesk", "admin": first.identity["admin"]}
    second = await Waha.link(druks_db, owner, identity=identity)

    assert response.status_code == 204
    assert resume.await_args.args[1].id == chat.id
    await druks_db.refresh(first)
    assert await chat.is_held(druks_db)
    sessions = await Waha.list_sessions(druks_db, app="helpdesk", account_id=owner.id)
    assert [session.id for session in sessions] == [first.id, second.id]
    assert first.identity["number"] == "+41000000000"


async def test_a_waiting_run_asks_the_admin_and_its_end_reaches_the_chat(druks_db, helpdesk):
    owner = await bot_account(druks_db)
    connection = await link(druks_db, owner)
    chat = await Conversation.get_or_create_for_user(
        druks_db, connection, owner.id, **user(BEN, "Ben")
    )
    run = await seed_run(
        druks_db,
        kind="test",
        state="parked",
        input_gate="review",
        input_request={"presentation": "in_app", "controls": ["approve"], "questions": []},
    )
    run.conversation_id = chat.id
    run.input_requested_at = Base.utc_now()

    # With no admin person, the question goes to the phone's chat with itself.
    phone_chat = await druks_db.get(Conversation, await bot_service.ask_admin(druks_db, run))
    admin = await bot_service.add_admin(druks_db, connection, user(ANA, "Ana"))
    assert phone_chat.user_id == NUMBER
    assert await bot_service.ask_admin(druks_db, run) == admin.id
    assert await service.report_result(druks_db, run, result={"granted": True}) == chat.id
    await druks_db.refresh(admin, ["messages"])
    await druks_db.refresh(chat, ["messages"])
    assert run.id in admin.messages[-1].body
    assert '"granted":true' in chat.messages[-1].body
