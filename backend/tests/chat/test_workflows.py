from datetime import timedelta
from unittest import mock

from druks.accounts.models import Account
from druks.contrib.chat.app import Chat
from druks.contrib.chat.contracts import TurnOutput
from druks.contrib.chat.enums import Role
from druks.contrib.chat.models import Conversation
from druks.contrib.chat.workflows import ChatTurn, ConfirmTool, Talk
from druks.notifications.services import validate_in_app_answer
from druks.workflows import current_workflow

_CHAT_TURN_ASK = {
    "presentation": "in_app",
    "label": "Message",
    "controls": ["send", "stop"],
    "questions": [],
}


def test_chat_turn_accepts_the_in_app_send_and_stop_payload():
    send = validate_in_app_answer(_CHAT_TURN_ASK, "send", {}, "and then?")
    assert ChatTurn.model_validate(send).action == "send"
    assert ChatTurn.model_validate(send).note == "and then?"
    stop = validate_in_app_answer(_CHAT_TURN_ASK, "stop", {}, "")
    assert ChatTurn.model_validate(stop).action == "stop"


async def _run_talk(conversation: Conversation, monkeypatch) -> None:
    monkeypatch.setattr(Talk, "record_message", Talk.record_message.__wrapped__)
    monkeypatch.setattr(Talk, "name_thread", Talk.name_thread.__wrapped__)
    flow = Talk()
    flow.subject = conversation
    flow.account_id = conversation.account_id
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
        return ChatTurn(action="stop")

    monkeypatch.setattr(ChatTurn, "wait", classmethod(wait))
    monkeypatch.setattr(Talk, "deferred_writes", mock.AsyncMock(return_value=[]))

    await _run_talk(conversation, monkeypatch)

    reply.assert_awaited_once()
    assert waits[0]["hold_sandbox"] == timedelta(minutes=15)
    assert waits[0]["input_request"] == _CHAT_TURN_ASK
    messages = await conversation.list_messages()
    assert [message.body for message in messages] == ["hello", "hi"]
    assert [message.role for message in messages] == [Role.USER, Role.ASSISTANT]
    assert conversation.title == "hello"


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
    answers = iter(
        [ChatTurn(action="send", note="and then?"), ChatTurn(action="stop", note="leave unused")]
    )

    async def wait(cls, **kwargs):
        return next(answers)

    monkeypatch.setattr(ChatTurn, "wait", classmethod(wait))
    monkeypatch.setattr(Talk, "deferred_writes", mock.AsyncMock(return_value=[]))

    await _run_talk(conversation, monkeypatch)

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
    assert conversation.title == "hello"


async def test_stop_does_not_write_a_message(druks_db, monkeypatch):
    account = await Account.get_or_create("op@example.com")
    conversation = await Conversation.create(account_id=account.id, title="")
    await conversation.add_message(role=Role.USER, body="hello")
    monkeypatch.setattr(Chat, "reply", mock.AsyncMock(return_value=TurnOutput(text="hi")))
    monkeypatch.setattr(Talk, "deferred_writes", mock.AsyncMock(return_value=[]))

    async def wait(cls, **kwargs):
        return ChatTurn(action="stop", note="goodnight")

    monkeypatch.setattr(ChatTurn, "wait", classmethod(wait))

    await _run_talk(conversation, monkeypatch)

    assert [message.body for message in await conversation.list_messages()] == ["hello", "hi"]


async def test_talk_names_an_untitled_thread_once(druks_db, monkeypatch):
    account = await Account.get_or_create("op@example.com")
    conversation = await Conversation.create(account_id=account.id, title="Pump")
    await conversation.add_message(role=Role.USER, body="hello")
    monkeypatch.setattr(Chat, "reply", mock.AsyncMock(return_value=TurnOutput(text="hi")))
    monkeypatch.setattr(Talk, "deferred_writes", mock.AsyncMock(return_value=[]))

    async def wait(cls, **kwargs):
        return ChatTurn(action="stop")

    monkeypatch.setattr(ChatTurn, "wait", classmethod(wait))

    await _run_talk(conversation, monkeypatch)

    assert conversation.title == "Pump"


async def test_talk_prompts_with_bounded_history(druks_db, monkeypatch):
    account = await Account.get_or_create("op@example.com")
    conversation = await Conversation.create(account_id=account.id, title="t")
    for index in range(41):
        await conversation.add_message(role=Role.USER, body=f"m{index}")
    turns: list[dict] = []

    async def reply(**kwargs):
        turns.append(kwargs)
        return TurnOutput(text="ok")

    monkeypatch.setattr(Chat, "reply", staticmethod(reply))

    async def wait(cls, **kwargs):
        return ChatTurn(action="stop")

    monkeypatch.setattr(ChatTurn, "wait", classmethod(wait))
    monkeypatch.setattr(Talk, "deferred_writes", mock.AsyncMock(return_value=[]))

    await _run_talk(conversation, monkeypatch)

    assert [message["body"] for message in turns[0]["messages"]] == [
        f"m{index}" for index in range(1, 41)
    ]


async def test_talk_confirms_deferred_writes_before_the_next_line(druks_db, monkeypatch):
    account = await Account.get_or_create("op@example.com")
    conversation = await Conversation.create(account_id=account.id, title="")
    await conversation.add_message(role=Role.USER, body="hello")

    monkeypatch.setattr(Chat, "reply", mock.AsyncMock(return_value=TurnOutput(text="hi")))
    proposed = [
        {
            "method": "POST",
            "path": "/api/gates/run-1/answer",
            "body": "{}",
            "content_type": "application/json",
        }
    ]
    monkeypatch.setattr(Talk, "deferred_writes", mock.AsyncMock(return_value=proposed))
    applied = []

    async def apply(self, writes):
        applied.extend(writes)

    monkeypatch.setattr(Talk, "apply_deferred_writes", apply)
    parked = []

    async def confirm_wait(cls, **kwargs):
        parked.append(kwargs)
        return ConfirmTool(action="approve")

    async def turn_wait(cls, **kwargs):
        return ChatTurn(action="stop")

    monkeypatch.setattr(ConfirmTool, "wait", classmethod(confirm_wait))
    monkeypatch.setattr(ChatTurn, "wait", classmethod(turn_wait))

    await _run_talk(conversation, monkeypatch)

    assert parked[0]["input_request"]["controls"] == ["approve", "reject"]
    assert applied == proposed


async def test_talk_skips_deferred_writes_when_the_operator_rejects(druks_db, monkeypatch):
    account = await Account.get_or_create("op@example.com")
    conversation = await Conversation.create(account_id=account.id, title="")
    await conversation.add_message(role=Role.USER, body="hello")

    monkeypatch.setattr(Chat, "reply", mock.AsyncMock(return_value=TurnOutput(text="hi")))
    monkeypatch.setattr(
        Talk,
        "deferred_writes",
        mock.AsyncMock(
            return_value=[{"method": "POST", "path": "/x", "body": "", "content_type": ""}]
        ),
    )
    apply = mock.AsyncMock()
    monkeypatch.setattr(Talk, "apply_deferred_writes", apply)

    async def confirm_wait(cls, **kwargs):
        return ConfirmTool(action="reject")

    async def turn_wait(cls, **kwargs):
        return ChatTurn(action="stop")

    monkeypatch.setattr(ConfirmTool, "wait", classmethod(confirm_wait))
    monkeypatch.setattr(ChatTurn, "wait", classmethod(turn_wait))

    await _run_talk(conversation, monkeypatch)

    apply.assert_not_awaited()
