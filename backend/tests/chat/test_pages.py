from datetime import UTC, datetime

from druks.accounts.models import Account
from druks.contrib.chat.enums import Role
from druks.contrib.chat.models import Conversation
from druks.contrib.chat.workflows import ChatTurn, Talk
from druks.testing import seed_run


async def test_the_roster_names_chat_pages(druks_client):
    roster = {entry["name"]: entry for entry in (await druks_client.get("/api/apps")).json()}
    pages = roster["chat"]["pages"]
    by_name = {page["name"]: page for page in pages}
    assert set(by_name) == {"list", "new", "thread", "settings"}
    assert by_name["list"]["path"] == "/chat"
    assert by_name["new"]["path"] == "/chat/new"
    assert by_name["thread"]["path"] == "/chat/conversations/{conversation_id}"
    assert by_name["settings"]["path"] == "/chat/conversations/{conversation_id}/settings"
    assert by_name["settings"]["parent"] == "thread"
    assert by_name["list"]["parent"] == ""
    assert by_name["list"]["label"] == "Conversations"


async def test_the_list_page_shows_this_accounts_threads(druks_client):
    owner = await Account.get_or_create("op@example.com")
    other = await Account.get_or_create("dev@example.com")
    mine = await Conversation.create(account_id=owner.id, title="Pump")
    await Conversation.create(account_id=other.id, title="theirs")

    page = (await druks_client.get("/api/chat/pages")).json()

    assert page["title"] == "Conversations"
    assert page["controls"][0]["page"] == "new"
    (cards,) = page["blocks"]
    (card,) = cards["cards"]
    assert card["title"] == "Pump"
    assert card["controls"][0] == {
        "block": "link",
        "label": "Open",
        "page": "thread",
        "arguments": {"conversation_id": str(mine.id)},
        "url": "",
        "subject": None,
    }


async def test_the_list_page_empty_state_points_at_new(druks_client):
    page = (await druks_client.get("/api/chat/pages")).json()

    (cards,) = page["blocks"]
    assert cards["cards"] == []
    assert cards["empty"]["controls"][0]["page"] == "new"


async def test_the_new_page_collects_an_optional_title_and_a_required_message(druks_client):
    page = (await druks_client.get("/api/chat/pages/new")).json()

    (form,) = page["blocks"]
    assert form["block"] == "form"
    assert [field["name"] for field in form["fields"]] == ["title", "body"]
    assert form["fields"][0]["isRequired"] is False
    assert form["fields"][1]["isRequired"] is True
    assert form["action"]["operation"] == "create_conversation"


async def test_the_thread_shows_messages_and_follows_the_conversation(druks_client):
    account = await Account.get_or_create("op@example.com")
    conversation = await Conversation.create(account_id=account.id, title="Pump")
    await conversation.add_message(role=Role.USER, body="hello")

    page = (await druks_client.get(f"/api/chat/pages/conversations/{conversation.id}")).json()

    assert page["title"] == "Pump"
    region = page["blocks"][0]
    assert region["follows"] == {
        "subjectType": "conversation",
        "subjectId": str(conversation.id),
    }
    (cards, *turn) = region["blocks"]
    assert turn == []
    (card,) = cards["cards"]
    assert card["title"] == "You"
    assert card["blocks"][0] == {"block": "quote", "text": "hello"}
    assert page["controls"][1]["subject"] == {
        "subjectType": "conversation",
        "subjectId": str(conversation.id),
    }


async def test_a_parked_turn_puts_gate_controls_on_the_thread(druks_client, druks_db):
    account = await Account.get_or_create("op@example.com")
    conversation = await Conversation.create(account_id=account.id, title="")
    run = await seed_run(
        druks_db,
        kind=Talk.kind,
        subject=conversation,
        state="parked",
        input_gate=ChatTurn.name,
        input_request={
            "presentation": "in_app",
            "label": "Message",
            "controls": ["send", "stop"],
            "questions": [],
        },
    )
    run.input_requested_at = datetime.now(UTC)
    await druks_db.flush()

    page = (await druks_client.get(f"/api/chat/pages/conversations/{conversation.id}")).json()

    region = page["blocks"][0]
    assert region["blocks"][-1] == {"block": "gate_controls", "run": run.id}


async def test_a_running_turn_shows_status_not_a_gate(druks_client, druks_db):
    account = await Account.get_or_create("op@example.com")
    conversation = await Conversation.create(account_id=account.id, title="")
    await seed_run(druks_db, kind=Talk.kind, subject=conversation, state="running")

    page = (await druks_client.get(f"/api/chat/pages/conversations/{conversation.id}")).json()

    region = page["blocks"][0]
    assert region["blocks"][-1]["block"] == "text"
    assert all(block["block"] != "gate_controls" for block in region["blocks"])


async def test_another_operators_thread_is_an_empty_state(druks_client):
    other = await Account.get_or_create("dev@example.com")
    conversation = await Conversation.create(account_id=other.id, title="secret")
    await conversation.add_message(role=Role.USER, body="nope")

    page = (await druks_client.get(f"/api/chat/pages/conversations/{conversation.id}")).json()

    assert page["blocks"][0]["block"] == "empty_state"
    assert "secret" not in str(page)
    assert "nope" not in str(page)

    settings = (
        await druks_client.get(f"/api/chat/pages/conversations/{conversation.id}/settings")
    ).json()
    assert settings["blocks"][0]["block"] == "empty_state"


async def test_settings_offers_the_autonomy_modes(druks_client):
    account = await Account.get_or_create("op@example.com")
    conversation = await Conversation.create(account_id=account.id, title="Pump")

    page = (
        await druks_client.get(f"/api/chat/pages/conversations/{conversation.id}/settings")
    ).json()

    (form,) = page["blocks"]
    assert form["action"]["operation"] == "set_autonomy"
    assert form["fields"][0]["name"] == "autonomy"
    assert [option["value"] for option in form["fields"][0]["options"]] == [
        "propose",
        "confirm",
        "full",
    ]
