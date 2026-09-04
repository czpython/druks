from datetime import timedelta
from unittest import mock

from druks.accounts.models import Account
from druks.contrib.chat.app import Chat
from druks.contrib.chat.contracts import TurnOutput
from druks.contrib.chat.enums import Role
from druks.contrib.chat.models import Conversation
from druks.contrib.chat.workflows import ChatTurn, Talk
from druks.workflows import current_workflow


async def _run_talk(conversation: Conversation) -> None:
    flow = Talk()
    flow.subject = conversation
    token = current_workflow.set(flow)
    try:
        await flow.run_multistep()
    finally:
        current_workflow.reset(token)


async def test_dispatch_starts_talk_for_the_conversation(monkeypatch):
    conversation = Conversation(id=42)
    start = mock.AsyncMock(return_value="run-1")
    monkeypatch.setattr(Talk, "start", staticmethod(start))

    run_id = await Talk.dispatch(conversation=conversation)

    assert run_id == "run-1"
    start.assert_awaited_once_with(subject=conversation)


async def test_talk_appends_the_assistant_line_and_stops(druks_db, monkeypatch):
    account = await Account.get_or_create("op@example.com")
    conversation = await Conversation.create(account_id=account.id, title="")
    await conversation.add_message(role=Role.USER, body="hello")

    reply = mock.AsyncMock(return_value=TurnOutput(text="hi"))
    monkeypatch.setattr(Chat, "reply", staticmethod(reply))
    waits: list[dict] = []

    async def wait(cls, **kwargs):
        waits.append(kwargs)
        return ChatTurn(text="", stop=True)

    monkeypatch.setattr(ChatTurn, "wait", classmethod(wait))
    monkeypatch.setattr(Talk, "record_message", Talk.record_message.__wrapped__)

    await _run_talk(conversation)

    reply.assert_awaited_once()
    assert waits[0]["hold_sandbox"] == timedelta(minutes=15)
    assert waits[0]["input_request"] == {"presentation": "in_app", "label": "Chat turn"}
    messages = await conversation.list_messages()
    assert [message.body for message in messages] == ["hello", "hi"]
    assert [message.role for message in messages] == [Role.USER, Role.ASSISTANT]


async def test_talk_appends_the_operator_line_and_loops(druks_db, monkeypatch):
    account = await Account.get_or_create("op@example.com")
    conversation = await Conversation.create(account_id=account.id, title="")
    await conversation.add_message(role=Role.USER, body="hello")

    outputs = iter([TurnOutput(text="hi"), TurnOutput(text="ok")])
    turns: list[dict] = []

    async def reply(**kwargs):
        turns.append(kwargs)
        return next(outputs)

    monkeypatch.setattr(Chat, "reply", staticmethod(reply))
    answers = iter([ChatTurn(text="and then?", stop=False), ChatTurn(text="", stop=True)])

    async def wait(cls, **kwargs):
        return next(answers)

    monkeypatch.setattr(ChatTurn, "wait", classmethod(wait))
    monkeypatch.setattr(Talk, "record_message", Talk.record_message.__wrapped__)

    await _run_talk(conversation)

    messages = await conversation.list_messages()
    assert [message.body for message in messages] == ["hello", "hi", "and then?", "ok"]
    assert [message.role for message in messages] == [
        Role.USER,
        Role.ASSISTANT,
        Role.USER,
        Role.ASSISTANT,
    ]
    assert turns[0]["autonomy"] == conversation.autonomy
    assert turns[1]["messages"][-1] == {"role": Role.USER, "body": "and then?"}
