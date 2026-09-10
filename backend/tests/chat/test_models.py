from druks.accounts.models import Account
from druks.apps.loader import get_app
from druks.contrib.chat.enums import Autonomy, Role
from druks.contrib.chat.models import Conversation, Message


def test_chat_app_is_bundled_with_prefixed_tables():
    app = get_app("chat")
    assert app.name == "chat"
    assert app.prefix_tables is True
    assert app.table_prefix == "chat_"


async def test_list_for_account_excludes_another_operators_threads():
    owner = await Account.get_or_create("op@example.com")
    other = await Account.get_or_create("dev@example.com")
    mine = await Conversation.create(account_id=owner.id, title="mine")
    await Conversation.create(account_id=other.id, title="theirs")

    listed = await Conversation.list_for_account(owner.id)
    assert [conversation.id for conversation in listed] == [mine.id]
    assert listed[0].title == "mine"
    assert listed[0].autonomy == Autonomy.PROPOSE
    assert listed[0].account_id == owner.id


async def test_conversations_list_newest_first():
    account = await Account.get_or_create("op@example.com")
    first = await Conversation.create(account_id=account.id, title="first")
    second = await Conversation.create(
        account_id=account.id, title="second", autonomy=Autonomy.FULL
    )

    listed = await Conversation.list_for_account(account.id)
    assert [conversation.id for conversation in listed] == [second.id, first.id]
    assert listed[0].autonomy == Autonomy.FULL


async def test_messages_are_rows_and_empty_is_a_list():
    account = await Account.get_or_create("op@example.com")
    conversation = await Conversation.create(account_id=account.id, title="quiet")

    assert await conversation.list_messages() == []

    first = await conversation.add_message(role=Role.USER, body="hello")
    second = await conversation.add_message(role=Role.ASSISTANT, body="hi")
    listed = await conversation.list_messages()
    assert [message.body for message in listed] == ["hello", "hi"]
    assert [message.role for message in listed] == [Role.USER, Role.ASSISTANT]
    assert listed[0].id == first.id
    assert listed[1].id == second.id
    assert all(isinstance(message, Message) for message in listed)


async def test_list_summaries_is_this_accounts_threads():
    owner = await Account.get_or_create("op@example.com")
    other = await Account.get_or_create("dev@example.com")
    mine = await Conversation.create(account_id=owner.id, title="mine")
    await Conversation.create(account_id=other.id, title="theirs")

    listed = await Conversation.list_summaries(owner.id)
    assert [summary.id for summary in listed] == [str(mine.id)]
    assert listed[0].title == "mine"
    assert listed[0].autonomy == Autonomy.PROPOSE
    assert listed[0].label == mine.label
    assert await Conversation.list_summaries(None) == []
