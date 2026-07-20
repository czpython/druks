from datetime import UTC, datetime, timedelta
from typing import Any

from druks.accounts.models import Account
from druks.core.utils.time import operator_local_day
from druks.database import db_session
from druks.durable.enums import RunState
from druks.durable.models import AgentCall, Artifact, Run
from druks.durable.reads import read_slice
from druks.durable.schemas import AgentCallSummary
from druks.harnesses.artifacts import normalize_token_usage
from druks.harnesses.models import HarnessConnection
from druks.harnesses.registry import get_harnesses
from druks.mcp.gateway.exceptions import (
    AgentCallNotFound,
    GateNotAnswerable,
    GateNotOpen,
    GateRoundStale,
    InvalidGateAnswer,
    RunNotActive,
    RunNotFound,
)
from druks.mcp.gateway.schemas import (
    AgentCallDetail,
    AgentHarnessUsage,
    AgentUsage,
    ArtifactChunk,
    CancelRunResult,
    GateAnswerResult,
    GateDetail,
)
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
_NOTE_BYTES = 2 * 1024
_HISTORY_POINTS = 8  # trend points per harness on the usage response


def get_gate(run_id: str) -> GateDetail:
    run = Run.get(run_id)
    if not run:
        raise RunNotFound(run_id)
    if run.state != RunState.PENDING_INPUT.value:
        raise GateNotOpen(run_id)
    ask = run.input_request
    if not ask or ask.get("presentation") != "in_app":
        # External gates are answered on their source (PR review, ticket
        # comment); an ask-less park offers no reply contract either.
        raise GateNotAnswerable(run_id)
    ask = run.get_ask()
    return GateDetail(
        run_id=run.id,
        gate=run.input_gate,  # type: ignore[arg-type]
        parked_at=run.input_requested_at,  # type: ignore[arg-type]
        ask=ask,
        artifact=_artifact_chunk(Artifact.get_latest_for_run(run.id)),
        reply_schema=_reply_schema(ask),
    )


async def answer_gate(
    run_id: str, *, parked_at: datetime, control: str, answers: dict[str, str], note: str
) -> GateAnswerResult:
    run = Run.get(run_id)
    if not run:
        raise RunNotFound(run_id)
    # Same freshness discipline as notifications/services.py: the answer must
    # land on the run's live park, so the comparison reads fresh.
    db_session().expire(run)
    if run.answer_parked_at == parked_at:
        # The receipt names the parked_at an answer already resumed — a late
        # or duplicate answer collapses here instead of reading "not open".
        return GateAnswerResult(run_id=run.id, parked_at=parked_at, result="already_answered")
    if run.state != RunState.PENDING_INPUT.value:
        raise GateNotOpen(run_id)
    if run.input_requested_at != parked_at:
        raise GateRoundStale(run_id)
    ask = run.input_request
    if not ask or ask.get("presentation") != "in_app":
        raise GateNotAnswerable(run_id)
    if len(note.encode()) > _NOTE_BYTES:
        raise InvalidGateAnswer(f"note exceeds {_NOTE_BYTES} bytes")
    try:
        payload = validate_in_app_answer(run.get_ask(), control, answers, note)
    except InvalidChoiceError as error:
        # The one validation authority stays in notifications; this surface
        # speaks the agent taxonomy.
        raise InvalidGateAnswer(str(error)) from error
    # Run.resume keys the DBOS send by (gate, input_requested_at), so a
    # concurrent duplicate answer to the same parked_at collapses engine-side.
    await run.resume(**payload)
    return GateAnswerResult(run_id=run.id, parked_at=parked_at, result="answered")


def get_agent_call(call_id: str) -> AgentCallDetail:
    call = AgentCall.get(call_id)
    if not call:
        raise AgentCallNotFound(call_id)
    layout = call.artifact_layout
    return AgentCallDetail(
        run_id=call.run_id,
        call=AgentCallSummary.from_call(call),
        transcript=read_slice(
            layout.transcript, offset=-_TRANSCRIPT_TAIL_BYTES, limit=_TRANSCRIPT_TAIL_BYTES
        ),
        stderr=read_slice(layout.stderr, offset=-_STDERR_TAIL_BYTES, limit=_STDERR_TAIL_BYTES),
        artifact=_artifact_chunk(Artifact.get_for_call(call.id)),
    )


async def cancel_run(run_id: str, *, reason: str) -> CancelRunResult:
    run = Run.get(run_id)
    if not run:
        raise RunNotFound(run_id)
    if run.state == RunState.CANCELLED.value:
        return CancelRunResult(run_id=run.id, result="already_cancelled")
    if not run.is_active:
        raise RunNotActive(run_id)
    await run.cancel(failure=reason)
    return CancelRunResult(run_id=run.id, result="cancelled")


def _artifact_chunk(artifact: Artifact | None) -> ArtifactChunk | None:
    if not artifact:
        return
    call = AgentCall.get(artifact.agent_call_id)
    path = call.get_file_path(artifact.path) if call else None
    if not path:
        return
    return ArtifactChunk(
        call_id=artifact.agent_call_id,
        kind=artifact.kind,
        title=artifact.title,
        chunk=read_slice(path, offset=0, limit=_ARTIFACT_CHUNK_BYTES),
    )


def _reply_schema(ask: dict[str, Any]) -> dict[str, Any]:
    # What answer_gate accepts for this ask, as JSON Schema — the agent-facing
    # twin of validate_in_app_answer: a control from the offered vocabulary, an
    # answer per open question (an offered option id or the caller's own
    # words), and a free-text note.
    answers = {
        # pattern \S: the service strips whitespace, so a blank answer is
        # rejected — the schema says so up front.
        question["id"]: {"type": "string", "pattern": r"\S", "description": question["prompt"]}
        for question in ask.get("questions", [])
    }
    control: dict[str, Any] = {"type": "string", "enum": list(ask.get("controls", []))}
    if "request_changes" in control["enum"]:
        control["description"] = "request_changes needs an answer or a note to guide the re-plan."
    return {
        "type": "object",
        "properties": {
            "control": control,
            "answers": {"type": "object", "properties": answers, "additionalProperties": False},
            "note": {
                "type": "string",
                "maxLength": _NOTE_BYTES,
                "description": f"At most {_NOTE_BYTES} UTF-8 bytes.",
            },
        },
        "required": ["control"],
        "additionalProperties": False,
    }


def get_usage(account: Account) -> AgentUsage:
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
    return AgentUsage(
        day=local_start.date().isoformat(),
        timezone=str(timezone),
        spend_today_usd=round(spend, 4),
        tokens_today=tokens,
        runs_today=len(rows),
        harnesses=[_harness_usage(h.name, account.id, now=now) for h in get_harnesses()],
    )


def _harness_usage(name: str, account_id: str, *, now: datetime) -> AgentHarnessUsage:
    is_connected = bool(HarnessConnection.get_for_account(name, account_id))
    row = UsageScrape.latest_for(name, account_id)
    if not row:
        return AgentHarnessUsage(name=name, is_connected=is_connected)
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
    return AgentHarnessUsage(
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
