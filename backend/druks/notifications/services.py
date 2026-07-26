from typing import Any

from druks.database import db_session
from druks.durable.enums import RunState
from druks.durable.models import Run
from druks.notifications.exceptions import (
    AlreadyAcknowledgedError,
    CorruptCorrelationError,
    InvalidChoiceError,
    StaleRoundError,
    UnknownTokenError,
)
from druks.notifications.models import Notification


def validate_in_app_answer(
    ask: dict[str, Any], control: str, answers: dict[str, str], note: str
) -> dict[str, Any]:
    # The in-app answer contract, shared by the runs resume route and the
    # notification respond rail. The control must be one the ask offered — the
    # vocabulary is workflow-owned, never read from the client, so a spoofed
    # control can't drive control flow.
    if control not in ask.get("controls", []):
        raise InvalidChoiceError(f"unknown control {control!r}")
    if control == "request_changes" and not answers and not note.strip():
        # request_changes exists to redirect the next pass; empty-handed it
        # would only re-run the same plan blind.
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


async def respond_to_notification(token: str, choice: dict[str, Any]) -> None:
    notification = Notification.get_for_token(token)
    if not notification:
        raise UnknownTokenError()
    if notification.is_acknowledged:
        raise AlreadyAcknowledgedError()
    if not notification.run_id:
        # A run-less notification routes no reply.
        raise InvalidChoiceError("this notification does not take an answer")
    run = Run.get(notification.run_id)
    if not run:
        raise CorruptCorrelationError(notification.id, notification.run_id)
    # The notification snapshots the round it was sent for; the answer must
    # land on the run's live round — expire so the comparison reads fresh.
    db_session().expire(run)
    if run.state != RunState.PARKED.value or run.input_requested_at != notification.run_parked_at:
        raise StaleRoundError()
    ask = run.get_ask()
    if ask.get("presentation") != "in_app":
        # External gates are answered on their source (PR review, ticket
        # comment) via the existing webhook paths, never through this rail —
        # and an ask that doesn't declare in_app isn't answerable either
        # (the same declared-key read get_ask itself dispatches on).
        raise InvalidChoiceError("this notification is informational; answer on its source")
    resume_payload = validate_in_app_answer(
        ask, choice["control"], choice.get("answers", {}), choice.get("note", "")
    )
    await run.resume(**resume_payload)
    if not notification.mark_acknowledged():
        # A concurrent responder won the claim; this send already collapsed on
        # the DBOS round key.
        raise AlreadyAcknowledgedError()
