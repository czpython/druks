from datetime import UTC, datetime, timedelta

from druks.accounts.models import Account
from druks.core.utils.time import operator_local_day
from druks.database import db_session
from druks.durable.enums import RunState
from druks.durable.models import AgentCall, Artifact, Run
from druks.durable.reads import read_slice
from druks.durable.schemas import AgentCallResponse
from druks.harnesses.artifacts import normalize_token_usage
from druks.harnesses.models import HarnessConnection
from druks.harnesses.registry import get_harnesses
from druks.mcp.gateway import exceptions, schemas
from druks.notifications.exceptions import InvalidChoiceError
from druks.notifications.services import validate_in_app_answer
from druks.usage.models import UsageScrape
from druks.usage.reads import list_finished_calls
from druks.usage.schemas import UsageHistoryPoint
from druks.usage.trends import FIVE_HOUR_RANGE, WEEK_RANGE, downsample
from druks.user_settings.models import UserSettings

_TRANSCRIPT_TAIL_BYTES = 8 * 1024
_STDERR_TAIL_BYTES = 4 * 1024
_ARTIFACT_CHUNK_BYTES = 4 * 1024
_HISTORY_POINTS = 8


def get_gate(run_id: str) -> schemas.GateResponse:
    run = Run.get(run_id)
    if not run:
        raise exceptions.RunNotFound(run_id)
    if run.state != RunState.PENDING_INPUT.value:
        raise exceptions.GateNotOpen(run_id)
    ask = run.input_request
    if not ask or ask.get("presentation") != "in_app":
        raise exceptions.GateNotAnswerable(run_id)
    return schemas.GateResponse(
        run_id=run.id,
        gate=run.input_gate,  # type: ignore[arg-type]
        parked_at=run.input_requested_at,  # type: ignore[arg-type]
        ask=run.get_ask(),
        artifact=_artifact_content(Artifact.get_latest_for_run(run.id)),
    )


async def answer_gate(
    run_id: str, *, parked_at: datetime, control: str, answers: dict[str, str], note: str
) -> schemas.GateAnswerResponse:
    run = Run.get(run_id)
    if not run:
        raise exceptions.RunNotFound(run_id)
    db_session().expire(run)  # the receipt/park comparison must read fresh
    if run.answer_parked_at == parked_at:
        return schemas.GateAnswerResponse(
            run_id=run.id, parked_at=parked_at, result="already_answered"
        )
    if run.state != RunState.PENDING_INPUT.value:
        raise exceptions.GateNotOpen(run_id)
    if run.input_requested_at != parked_at:
        raise exceptions.GateRoundStale(run_id)
    ask = run.input_request
    if not ask or ask.get("presentation") != "in_app":
        raise exceptions.GateNotAnswerable(run_id)
    try:
        payload = validate_in_app_answer(run.get_ask(), control, answers, note)
    except InvalidChoiceError as error:
        raise exceptions.InvalidGateAnswer(str(error)) from error
    await run.resume(**payload)
    return schemas.GateAnswerResponse(run_id=run.id, parked_at=parked_at, result="answered")


def get_agent_call(call_id: str) -> schemas.AgentCallDetailResponse:
    call = AgentCall.get(call_id)
    if not call:
        raise exceptions.AgentCallNotFound(call_id)
    layout = call.artifact_layout
    return schemas.AgentCallDetailResponse(
        run_id=call.run_id,
        call=AgentCallResponse.from_call(call),
        transcript=read_slice(
            layout.transcript, offset=-_TRANSCRIPT_TAIL_BYTES, limit=_TRANSCRIPT_TAIL_BYTES
        ).text,
        stderr=read_slice(layout.stderr, offset=-_STDERR_TAIL_BYTES, limit=_STDERR_TAIL_BYTES).text,
        artifact=_artifact_content(Artifact.get_for_call(call.id)),
    )


async def cancel_run(run_id: str, *, reason: str) -> schemas.CancelRunResponse:
    run = Run.get(run_id)
    if not run:
        raise exceptions.RunNotFound(run_id)
    if run.state == RunState.CANCELLED.value:
        return schemas.CancelRunResponse(run_id=run.id, result="already_cancelled")
    if not run.is_active:
        raise exceptions.RunNotActive(run_id)
    await run.cancel(failure=reason)
    return schemas.CancelRunResponse(run_id=run.id, result="cancelled")


def _artifact_content(artifact: Artifact | None) -> schemas.ArtifactContent | None:
    if not artifact:
        return
    call = AgentCall.get(artifact.agent_call_id)
    path = call.get_file_path(artifact.path) if call else None
    if not path:
        return
    return schemas.ArtifactContent(
        call_id=artifact.agent_call_id,
        kind=artifact.kind,
        title=artifact.title,
        content=read_slice(path, offset=0, limit=_ARTIFACT_CHUNK_BYTES).text,
    )


def get_usage(account: Account) -> schemas.AgentUsageResponse:
    now = datetime.now(UTC)
    timezone, local_start = operator_local_day(UserSettings.get().timezone, now)
    rows = list_finished_calls(account.id, since=local_start, until=local_start + timedelta(days=1))
    spend = 0.0
    tokens = 0
    for _, cost_usd, cost_metadata, _ in rows:
        if cost_usd is not None:
            spend += float(cost_usd)
        usage = normalize_token_usage(cost_metadata)
        if usage:
            tokens += usage["total_tokens"]
    return schemas.AgentUsageResponse(
        day=local_start.date().isoformat(),
        timezone=str(timezone),
        spend_today_usd=round(spend, 4),
        tokens_today=tokens,
        runs_today=len(rows),
        harnesses=[_harness_usage(h.name, account.id, now=now) for h in get_harnesses()],
    )


def _harness_usage(name: str, account_id: str, *, now: datetime) -> schemas.AgentHarnessUsage:
    is_connected = bool(HarnessConnection.get_for_account(name, account_id))
    row = UsageScrape.latest_for(name, account_id)
    if not row:
        return schemas.AgentHarnessUsage(name=name, is_connected=is_connected)
    history = UsageScrape.history_for(name, account_id, since=now - WEEK_RANGE)
    five_hour_cutoff = now - FIVE_HOUR_RANGE
    five_hour = [
        UsageHistoryPoint(t=point.scraped_at, pct=point.five_hour_percent_left)
        for point in history
        if point.five_hour_percent_left is not None and point.scraped_at >= five_hour_cutoff
    ]
    week = [
        UsageHistoryPoint(t=point.scraped_at, pct=point.week_percent_left)
        for point in history
        if point.week_percent_left is not None
    ]
    return schemas.AgentHarnessUsage(
        name=name,
        is_connected=is_connected,
        plan_tier=row.plan_tier,
        five_hour_percent_left=row.five_hour_percent_left,
        five_hour_resets_at=row.five_hour_resets_at,
        week_percent_left=row.week_percent_left,
        week_resets_at=row.week_resets_at,
        is_unlimited=row.unlimited,
        scraped_at=row.scraped_at,
        five_hour_history=downsample(five_hour, cap=_HISTORY_POINTS),
        week_history=downsample(week, cap=_HISTORY_POINTS),
    )
