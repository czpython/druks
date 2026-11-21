from dbos import DBOS
from sqlalchemy.ext.asyncio import AsyncSession

from druks.accounts.models import Account
from druks.apps.loader import get_app
from druks.apps.registry import workflows
from druks.chat.constants import CLICK_ANSWER_MESSAGE
from druks.chat.models import Conversation
from druks.chat.service import deliver
from druks.core.services import Slack
from druks.durable.enums import RunState
from druks.durable.models import Run
from druks.exceptions import ObjectNotFound
from druks.notifications.constants import TEAM_ONLY_MESSAGE
from druks.notifications.exceptions import (
    AlreadyAcknowledgedError,
    AnswerNotAllowedError,
    InvalidChoiceError,
    StaleRoundError,
    UnknownTokenError,
)
from druks.notifications.models import Notification
from druks.settings import get_settings
from druks.teams.models import Team


async def list_notification_channels() -> list[dict]:
    """The channels a person can choose for their notifications, by name. The bot posts only
    in a channel that it is a member of."""
    client = await Slack.get_client()
    return sorted(await client.list_member_channels(), key=lambda channel: channel["name"])


async def get_offered_control(ask: dict, control: str) -> dict:
    """The control that the ask offers under the id ``control``."""
    for offered in ask.get("controls", []):
        if offered["id"] == control:
            return offered
    raise InvalidChoiceError(f"unknown control {control!r}")


async def validate_in_app_answer(
    session: AsyncSession,
    run: Run,
    *,
    account_id: str | None,
    control: str,
    answers: dict[str, str],
    note: str,
) -> dict:
    # The in-app answer contract, shared by the runs resume route, the answer_gate
    # tool, and the notification respond rail. A question that a chat on an app's
    # channel asked is its admin's alone. A run of an app that a team lists is its team's
    # and the admins'. The control must be one the ask offered —
    # the vocabulary is workflow-owned, never read from the client, so a spoofed
    # control can't drive control flow.
    if run.conversation_id:
        conversation = await session.get(Conversation, run.conversation_id)
        if not conversation.is_answerable_by(account_id):
            raise AnswerNotAllowedError(
                "Only the admin of the number that started this run can answer it."
            )
    workflow = workflows.get(run.kind)
    if not workflow:
        raise ObjectNotFound("run", {"id": run.id})
    app = get_app(workflow.app) if workflow.app else None
    # A person with no account is on no team.
    if app and await Team.exists_for_app(session, app):
        account = await session.get(Account, account_id) if account_id else None
        if not account or not await account.can_use_app(app):
            reason = TEAM_ONLY_MESSAGE.format(app=app.name)
            # An installation with no endpoint has no address for the link.
            if endpoint := get_settings().urls.endpoint.rstrip("/"):
                reason += f" Open it in Druks: {endpoint}/runs/{run.id}"
            raise AnswerNotAllowedError(reason)
    ask = await run.get_ask()
    offered = await get_offered_control(ask, control)
    if offered["note_required"] and not answers and not note.strip():
        raise InvalidChoiceError(f"{control} needs an answer or a note to guide the re-plan")
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
    session: AsyncSession,
    token: str,
    choice: dict,
    *,
    user_id: str | None,
    account_id: str | None,
) -> Notification:
    """Answer the notification with a click by ``user_id``, whose account is
    ``account_id``, or with a link, which names nobody. In a chat, only its person answers.
    A click on the question of the chat's agent, or one that needs more, goes to the agent
    as the person's message."""
    notification = await Notification.get_for_token(session, token)
    if not notification:
        raise UnknownTokenError()
    if notification.is_acknowledged:
        raise AlreadyAcknowledgedError()
    run, ask = None, {}
    if notification.run_id:
        # The notification snapshots the round it was sent for; the answer must
        # land on the run's live round, so the read goes past the identity map.
        run = await session.get(Run, notification.run_id, populate_existing=True)
        if (
            run.state != RunState.PARKED.value
            or run.input_requested_at != notification.run_parked_at
        ):
            raise StaleRoundError()
        ask = await run.get_ask()
        if ask.get("presentation") != "in_app":
            # External gates are answered on their source (PR review, ticket
            # comment) via the existing webhook paths, never through this rail —
            # and an ask that doesn't declare in_app isn't answerable either
            # (the same declared-key read get_ask itself dispatches on).
            raise InvalidChoiceError("this notification is informational; answer on its source")
    elif not notification.conversation_id or not notification.actions:
        # Without a run, only the question of a chat's agent takes an answer.
        raise InvalidChoiceError("this notification does not take an answer")
    control = choice["control"]
    if notification.conversation_id:
        conversation = await session.get(Conversation, notification.conversation_id)
        # A link names nobody, so it answers no chat.
        if not user_id or user_id != conversation.user_id:
            raise AnswerNotAllowedError("Only the person of this conversation can answer it.")
        label = notification.get_action(control)["label"]
        if (
            not run
            or (await get_offered_control(ask, control))["note_required"]
            or ask["questions"]
        ):
            # A click on the agent's question, or on a control that needs more, reaches the
            # chat's agent as the person's message.
            if not await notification.mark_acknowledged():
                raise AlreadyAcknowledgedError()
            await conversation.create_message(session, label)
            await session.commit()
            await DBOS.start_workflow_async(deliver, conversation.id)
            return notification
        resume_payload = await validate_in_app_answer(
            session, run, account_id=conversation.account_id, control=control, answers={}, note=""
        )
        if not await notification.mark_acknowledged():
            raise AlreadyAcknowledgedError()
        await run.resume(**resume_payload)
        await conversation.create_message(
            session, CLICK_ANSWER_MESSAGE.format(run=run.id, answer=label), is_internal=True
        )
        return notification
    resume_payload = await validate_in_app_answer(
        session,
        run,
        account_id=account_id,
        control=control,
        answers=choice.get("answers", {}),
        note=choice.get("note", ""),
    )
    # Claim first: once the run resumes, close_notifications takes each message of the
    # question that is still open, and this one must show the click instead.
    if not await notification.mark_acknowledged():
        raise AlreadyAcknowledgedError()
    await run.resume(**resume_payload)
    return notification
