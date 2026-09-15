from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from druks.accounts.models import Account
from druks.api.exceptions import RunNotFound
from druks.core.utils.time import operator_local_day
from druks.durable.enums import RunState
from druks.durable.exceptions import AgentCallNotFound
from druks.durable.models import AgentCall, Artifact, Run
from druks.durable.reads import read_slice
from druks.durable.schemas import AgentCallResponse
from druks.harnesses.artifacts import normalize_token_usage
from druks.harnesses.providers import get_providers
from druks.mcp.gateway import exceptions, schemas
from druks.notifications.exceptions import InvalidChoiceError
from druks.notifications.services import validate_in_app_answer
from druks.secrets.datastructures import Audience
from druks.secrets.enums import SecretKind
from druks.secrets.models import VaultSecret
from druks.settings import load_settings
from druks.usage.models import UsageScrape
from druks.usage.reads import list_finished_calls
from druks.usage.schemas import UsageHistoryPoint
from druks.usage.trends import FIVE_HOUR_RANGE, WEEK_RANGE, downsample

_TRANSCRIPT_TAIL_BYTES = 8 * 1024
_STDERR_TAIL_BYTES = 4 * 1024
_ARTIFACT_CHUNK_BYTES = 4 * 1024
_HISTORY_POINTS = 8


async def get_gate(session: AsyncSession, run_id: str) -> schemas.GateResponse:
    run = await session.get(Run, run_id)
    if not run:
        raise RunNotFound(run_id)
    if run.state != RunState.PARKED.value:
        raise exceptions.GateNotOpen(run_id)
    ask = run.input_request
    if not ask or ask.get("presentation") != "in_app":
        raise exceptions.GateNotAnswerable(run_id)
    return schemas.GateResponse(
        run=run.id,
        gate=run.input_gate,  # type: ignore[arg-type]
        parked_at=run.input_requested_at,  # type: ignore[arg-type]
        ask=await run.get_ask(),
        artifact=await _artifact_content(
            session, await Artifact.get_latest_for_run(session, run.id)
        ),
    )


async def answer_gate(
    session: AsyncSession,
    run_id: str,
    *,
    parked_at: datetime,
    control: str,
    answers: dict[str, str],
    note: str,
) -> schemas.GateAnswerResponse:
    # Read past the identity map: the receipt/park comparison must see the row as it is.
    run = await session.get(Run, run_id, populate_existing=True)
    if not run:
        raise RunNotFound(run_id)
    if run.answer_parked_at == parked_at:
        return schemas.GateAnswerResponse(
            run=run.id, parked_at=parked_at, result="already_answered"
        )
    if run.state != RunState.PARKED.value:
        raise exceptions.GateNotOpen(run_id)
    if run.input_requested_at != parked_at:
        raise exceptions.GateRoundStale(run_id)
    ask = run.input_request
    if not ask or ask.get("presentation") != "in_app":
        raise exceptions.GateNotAnswerable(run_id)
    try:
        payload = validate_in_app_answer(await run.get_ask(), control, answers, note)
    except InvalidChoiceError as error:
        raise exceptions.InvalidGateAnswer(str(error)) from error
    await run.resume(**payload)
    return schemas.GateAnswerResponse(run=run.id, parked_at=parked_at, result="answered")


async def get_agent_call(session: AsyncSession, call_id: str) -> schemas.AgentCallDetailResponse:
    call = await AgentCall.get(session, call_id)
    layout = call.artifact_layout
    return schemas.AgentCallDetailResponse(
        run=call.run_id,
        call=AgentCallResponse.model_validate(call),
        transcript=read_slice(
            layout.transcript, offset=-_TRANSCRIPT_TAIL_BYTES, limit=_TRANSCRIPT_TAIL_BYTES
        ).text,
        stderr=read_slice(layout.stderr, offset=-_STDERR_TAIL_BYTES, limit=_STDERR_TAIL_BYTES).text,
        artifact=await _artifact_content(session, await Artifact.get_for_call(session, call.id)),
    )


async def _artifact_content(
    session: AsyncSession, artifact: Artifact | None
) -> schemas.ArtifactContent | None:
    if not artifact:
        return
    try:
        call = await AgentCall.get(session, artifact.agent_call_id)
    except AgentCallNotFound:
        return
    path = call.get_file_path(artifact.path)
    if not path:
        return
    return schemas.ArtifactContent(
        call_id=artifact.agent_call_id,
        kind=artifact.kind,
        title=artifact.title,
        content=read_slice(path, offset=0, limit=_ARTIFACT_CHUNK_BYTES).text,
    )


async def get_usage(session: AsyncSession, account: Account) -> schemas.AgentUsageResponse:
    now = datetime.now(UTC)
    timezone, local_start = operator_local_day(load_settings().timezone, now)
    rows = await list_finished_calls(
        session, account.id, since=local_start, until=local_start + timedelta(days=1)
    )
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
        providers=[
            await _provider_usage(session, p.id, account.id, now=now) for p in get_providers()
        ],
    )


async def _provider_usage(
    session: AsyncSession, provider_id: str, account_id: str, *, now: datetime
) -> schemas.AgentProviderUsage:
    is_connected = bool(
        await VaultSecret.lookup(
            session, SecretKind.SUBSCRIPTION, Audience.provider(provider_id), account_id
        )
    )
    row = await UsageScrape.latest_for(session, provider_id, account_id)
    if not row:
        return schemas.AgentProviderUsage(id=provider_id, is_connected=is_connected)
    history = await UsageScrape.history_for(
        session, provider_id, account_id, since=now - WEEK_RANGE
    )
    five_hour_cutoff = now - FIVE_HOUR_RANGE
    five_hour = [
        UsageHistoryPoint(t=point.scraped_at, pct=point.five_hour_percent_left)
        for point in history
        if point.five_hour_percent_left is not None and point.scraped_at >= five_hour_cutoff
    ]
    week = [
        UsageHistoryPoint(t=point.scraped_at, pct=binding["percent_left"])
        for point in history
        if (binding := point.binding_week())
    ]
    binding_week = row.binding_week() or {}
    return schemas.AgentProviderUsage(
        id=provider_id,
        is_connected=is_connected,
        plan_tier=row.plan_tier,
        five_hour_percent_left=row.five_hour_percent_left,
        five_hour_resets_at=row.five_hour_resets_at,
        week_percent_left=binding_week.get("percent_left"),
        week_resets_at=binding_week.get("resets_at"),
        is_unlimited=row.unlimited,
        scraped_at=row.scraped_at,
        five_hour_history=downsample(five_hour, cap=_HISTORY_POINTS),
        week_history=downsample(week, cap=_HISTORY_POINTS),
    )
