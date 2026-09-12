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
    monkeypatch.setattr(Talk, "settle_proposals", Talk.settle_proposals.__wrapped__)

    flow = Talk()
    flow.subject = conversation
    flow.account_id = conversation.account_id
    token = current_workflow.set(flow)
    try:
        await flow.run_multistep()
    finally:
        current_workflow.reset(token)


def _no_proposals(monkeypatch) -> None:
    monkeypatch.setattr(Talk, "take_proposals", mock.AsyncMock(return_value=[]))


async def test_dispatch_starts_talk_for_the_conversation(monkeypatch):
    conversation = Conversation(id=42)
    start = mock.AsyncMock(return_value="run-1")
    monkeypatch.setattr(Talk, "start", staticmethod(start))

    run_id = await Talk.dispatch(conversation=conversation)

    assert run_id == "run-1"
    start.assert_awaited_once_with(subject=conversation)


async def test_talk_appends_the_assistant_line_and_stops(druks_db, monkeypatch):
    account = await Account.get_or_create("op@example.com")
    conversation = await Conversation.start(account_id=account.id, body="hello")

    reply = mock.AsyncMock(return_value=TurnOutput(text="hi"))
    monkeypatch.setattr(Chat, "reply", staticmethod(reply))
    waits: list[dict] = []

    async def wait(cls, **kwargs):
        waits.append(kwargs)
        return ChatTurn(action="stop")

    monkeypatch.setattr(ChatTurn, "wait", classmethod(wait))
    _no_proposals(monkeypatch)

    await _run_talk(conversation, monkeypatch)

    reply.assert_awaited_once()
    assert waits[0]["hold_sandbox"] == timedelta(minutes=15)
    assert waits[0]["input_request"] == _CHAT_TURN_ASK
    messages = await conversation.list_messages()
    assert [message.body for message in messages] == ["hello", "hi"]
    assert [message.role for message in messages] == [Role.USER, Role.ASSISTANT]


async def test_talk_appends_the_operator_line_and_loops(druks_db, monkeypatch):
    account = await Account.get_or_create("op@example.com")
    conversation = await Conversation.start(account_id=account.id, body="hello")

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
    _no_proposals(monkeypatch)

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


async def test_stop_does_not_write_a_message(druks_db, monkeypatch):
    account = await Account.get_or_create("op@example.com")
    conversation = await Conversation.start(account_id=account.id, body="hello")
    monkeypatch.setattr(Chat, "reply", mock.AsyncMock(return_value=TurnOutput(text="hi")))
    _no_proposals(monkeypatch)

    async def wait(cls, **kwargs):
        return ChatTurn(action="stop", note="goodnight")

    monkeypatch.setattr(ChatTurn, "wait", classmethod(wait))

    await _run_talk(conversation, monkeypatch)

    assert [message.body for message in await conversation.list_messages()] == ["hello", "hi"]


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
    _no_proposals(monkeypatch)

    await _run_talk(conversation, monkeypatch)

    assert [message["body"] for message in turns[0]["messages"]] == [
        f"m{index}" for index in range(1, 41)
    ]


def _proposal(path: str = "/api/gates/run-1/answer") -> dict[str, str]:
    return {"method": "POST", "path": path, "body": "{}", "content_type": "application/json"}


async def _answer_confirm(monkeypatch, action: str, parked: list[dict] | None = None):
    async def confirm_wait(cls, **kwargs):
        if parked is not None:
            parked.append(kwargs)
        return ConfirmTool(action=action)

    async def turn_wait(cls, **kwargs):
        return ChatTurn(action="stop")

    monkeypatch.setattr(ConfirmTool, "wait", classmethod(confirm_wait))
    monkeypatch.setattr(ChatTurn, "wait", classmethod(turn_wait))


async def test_an_approved_proposal_runs_and_says_so_on_the_thread(druks_db, monkeypatch):
    account = await Account.get_or_create("op@example.com")
    conversation = await Conversation.start(account_id=account.id, body="hello")
    monkeypatch.setattr(Chat, "reply", mock.AsyncMock(return_value=TurnOutput(text="hi")))
    proposed = [_proposal()]
    monkeypatch.setattr(Talk, "take_proposals", mock.AsyncMock(return_value=proposed))
    applied = mock.AsyncMock(return_value=[])
    monkeypatch.setattr(Talk, "apply_proposals", applied)
    parked: list[dict] = []
    await _answer_confirm(monkeypatch, "approve", parked)

    await _run_talk(conversation, monkeypatch)

    assert parked[0]["input_request"]["controls"] == ["approve", "reject"]
    assert parked[0]["hold_sandbox"] == timedelta(minutes=15)
    applied.assert_awaited_once_with(proposed)
    last = (await conversation.list_messages())[-1]
    assert last.role == Role.SYSTEM
    assert last.body == "1 of 1 approved actions ran."
    assert [message.role for message in await conversation.list_messages()] == [
        Role.USER,
        Role.ASSISTANT,
        Role.SYSTEM,
    ]


async def test_a_rejected_proposal_never_runs(druks_db, monkeypatch):
    account = await Account.get_or_create("op@example.com")
    conversation = await Conversation.start(account_id=account.id, body="hello")
    monkeypatch.setattr(Chat, "reply", mock.AsyncMock(return_value=TurnOutput(text="hi")))
    monkeypatch.setattr(Talk, "take_proposals", mock.AsyncMock(return_value=[_proposal()]))
    applied = mock.AsyncMock()
    monkeypatch.setattr(Talk, "apply_proposals", applied)
    await _answer_confirm(monkeypatch, "reject")

    await _run_talk(conversation, monkeypatch)

    applied.assert_not_awaited()
    assert (await conversation.list_messages())[-1].body == "1 proposed actions did not run."


async def test_a_proposal_that_fails_is_reported_and_the_thread_carries_on(druks_db, monkeypatch):
    """A refused write is the operator's to see. It is not the run's failure."""
    account = await Account.get_or_create("op@example.com")
    conversation = await Conversation.start(account_id=account.id, body="hello")
    monkeypatch.setattr(Chat, "reply", mock.AsyncMock(return_value=TurnOutput(text="hi")))
    monkeypatch.setattr(Talk, "take_proposals", mock.AsyncMock(return_value=[_proposal()]))
    monkeypatch.setattr(
        Talk,
        "apply_proposals",
        mock.AsyncMock(return_value=["POST /api/gates/run-1/answer: 409 already answered"]),
    )
    await _answer_confirm(monkeypatch, "approve")

    await _run_talk(conversation, monkeypatch)

    last = (await conversation.list_messages())[-1]
    assert last.body.startswith("0 of 1 approved actions ran.")
    assert "409 already answered" in last.body
