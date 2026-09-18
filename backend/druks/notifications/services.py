from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from druks.chat.models import Conversation
from druks.durable.enums import RunState
from druks.durable.models import Run
from druks.notifications.exceptions import (
    AlreadyAcknowledgedError,
    AnswerNotAllowedError,
    InvalidChoiceError,
    StaleRoundError,
    UnknownTokenError,
)
from druks.notifications.models import Notification


async def validate_in_app_answer(
    session: AsyncSession,
    run: Run,
    *,
    account_id: str | None,
    control: str,
    answers: dict[str, str],
    note: str,
) -> dict[str, Any]:
    # The in-app answer contract, shared by the runs resume route, the answer_gate
    # tool, and the notification respond rail. A question that a chat on an app's
    # channel asked is its admin's alone. The control must be one the ask offered —
    # the vocabulary is workflow-owned, never read from the client, so a spoofed
    # control can't drive control flow.
    if run.conversation_id:
        conversation = await session.get(Conversation, run.conversation_id)
        if not conversation.is_answerable_by(account_id):
            raise AnswerNotAllowedError()
    ask = await run.get_ask()
    if control not in ask.get("controls", []):
        raise InvalidChoiceError(f"unknown control {control!r}")
    if (
        control == "request_changes"
        and not answers
        and not note.strip()
        and not (ask.get("context") or "").strip()
    ):
        # Context is guidance owned by the parked round, so an empty reply can
        # still direct another pass when the ask carries it.
        raise InvalidChoiceError("request_changes needs an answer or a note to guide the re-plan")
    # Answers may be an offered option id or the operator's own words — free
    # text is content that flows into the next agent prompt, so only the
    # question ids are held to the ask; a blank answer is a client bug.
    # Blankness is checked stripped: the HTTP models normalize whitespace, but
    # this validator is also the direct-call boundary (the Slack rail).
    asked = {question["id"] for question in ask.get("questions", [])}
    for question_id, answer in answers.items():
        if question_id not in asked:
            raise InvalidChoiceError(f"answer to {question_id!r} matches no open question")
        if not answer.strip():
            raise InvalidChoiceError(f"blank answer to {question_id!r}")
    return {"action": control, "answers": answers, "note": note}


async def respond_to_notification(
    session: AsyncSession, token: str, choice: dict[str, Any]
) -> None:
    notification = await Notification.get_for_token(session, token)
    if not notification:
        raise UnknownTokenError()
    if notification.is_acknowledged:
        raise AlreadyAcknowledgedError()
    if not notification.run_id:
        # A run-less notification routes no reply.
        raise InvalidChoiceError("this notification does not take an answer")
    # The notification snapshots the round it was sent for; the answer must
    # land on the run's live round, so the read goes past the identity map.
    run = await session.get(Run, notification.run_id, populate_existing=True)
    if run.state != RunState.PARKED.value or run.input_requested_at != notification.run_parked_at:
        raise StaleRoundError()
    ask = await run.get_ask()
    if ask.get("presentation") != "in_app":
        # External gates are answered on their source (PR review, ticket
        # comment) via the existing webhook paths, never through this rail —
        # and an ask that doesn't declare in_app isn't answerable either
        # (the same declared-key read get_ask itself dispatches on).
        raise InvalidChoiceError("this notification is informational; answer on its source")
    # A notification button names no account.
    resume_payload = await validate_in_app_answer(
        session,
        run,
        account_id=None,
        control=choice["control"],
        answers=choice.get("answers", {}),
        note=choice.get("note", ""),
    )
    await run.resume(**resume_payload)
    if not await notification.mark_acknowledged():
        # A concurrent responder won the claim; this send already collapsed on
        # the DBOS round key.
        raise AlreadyAcknowledgedError()
