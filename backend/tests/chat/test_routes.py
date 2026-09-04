from druks.accounts.models import Account
from druks.contrib.chat.enums import Role
from druks.contrib.chat.models import Conversation
from druks.contrib.chat.workflows import Talk


async def test_create_conversation_posts_body_and_starts_talk(druks_client, monkeypatch):
    started = []

    async def dispatch(*, conversation):
        started.append(conversation.id)
        return "run-1"

    monkeypatch.setattr(Talk, "dispatch", staticmethod(dispatch))

    created = await druks_client.post(
        "/api/chat/conversations",
        json={"body": "hello"},
    )

    assert created.status_code == 201
    conversation = await Conversation.get(created.json()["id"])
    assert conversation.title == ""
    account = await Account.get_or_create("op@example.com")
    assert conversation.account_id == account.id
    assert started == [conversation.id]
    messages = await conversation.list_messages()
    assert [message.body for message in messages] == ["hello"]
    assert [message.role for message in messages] == [Role.USER]


async def test_a_later_post_starts_another_conversation_not_a_new_talk_on_the_first(
    druks_client, monkeypatch
):
    started = []

    async def dispatch(*, conversation):
        started.append(conversation.id)
        return "run"

    monkeypatch.setattr(Talk, "dispatch", staticmethod(dispatch))

    first = await druks_client.post("/api/chat/conversations", json={"body": "hello"})
    second = await druks_client.post("/api/chat/conversations", json={"body": "again"})

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    assert started == [first.json()["id"], second.json()["id"]]
