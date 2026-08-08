# The durable read side: every query that composes rows into response shapes.
# Schemas stay pure projections; routes call in here.
import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from sqlalchemy import Engine

from druks.database import session_scope
from druks.durable.activity import get_run_phase
from druks.durable.live import keepalive_comment, serialize_model_event

from .enums import RunState
from .models import AgentCall, Artifact, Run
from .schemas import (
    AgentCallFiles,
    AgentCallResponse,
    RunResponse,
    SubjectActivity,
    SubjectResponse,
    SubjectStatus,
    SubjectSummary,
    TextSlice,
    TranscriptChunk,
)

if TYPE_CHECKING:
    from druks.workflows import Workflow

_TRANSCRIPT_POLL_SECONDS = 0.5
_TRANSCRIPT_KEEPALIVE_SECONDS = 15.0
_TERMINAL_CALL_STATES = {"succeeded", "failed", "abandoned"}


def get_agent_call_files(call_id: str) -> AgentCallFiles | None:
    call = AgentCall.get(call_id)
    if not call:
        return
    return AgentCallFiles.from_call(call, Artifact.get_for_call(call.id))


def list_subject_timeline(subject_type: str, subject_id: str) -> list[RunResponse]:
    # The subject's whole timeline: every run about it, oldest first, each
    # with its agent calls.
    runs = Run.list_for_subject(subject_type, subject_id)
    return _timeline(runs, AgentCall.get_by_run([run.id for run in runs]))


def get_subject_status(
    subject_type: str, subject_id: str, *, workflow: "type[Workflow] | None" = None
) -> SubjectStatus:
    kind = workflow.kind if workflow else None
    latest = Run.get_latest_for_subject(subject_type, subject_id, kind=kind)
    return _status(latest, _running_calls(latest))


async def get_subject_phase(subject_type: str, subject_id: str) -> str | None:
    runs = Run.list_for_subject(subject_type, subject_id)
    active_run = next((run for run in runs if run.is_active), None)
    if active_run and active_run.is_running:
        return await get_run_phase(active_run.id)
    return


def get_subject_response(
    subject_type: str,
    subject_id: str,
    *,
    summary: SubjectSummary,
    activity: SubjectActivity | None = None,
) -> SubjectResponse:
    runs = Run.list_for_subject(subject_type, subject_id)
    calls_by_run = AgentCall.get_by_run([run.id for run in runs])
    latest = Run.get_latest_for_subject(subject_type, subject_id)
    return SubjectResponse(
        summary=summary,
        status=_status(latest, calls_by_run.get(latest.id, []) if latest else []),
        timeline=_timeline(runs, calls_by_run),
        activity=activity,
    )


def _timeline(runs: list[Run], calls_by_run: dict[str, list[AgentCall]]) -> list[RunResponse]:
    # get_by_run keys every run id, so the lookup is total.
    ordered = sorted(runs, key=lambda run: (run.created_at, run.id))
    return [
        RunResponse.from_run(
            run,
            calls_by_run[run.id],
            input_request=run.get_ask() if run.input_request else None,
        )
        for run in ordered
    ]


def _running_calls(run: Run | None) -> list[AgentCall]:
    # A parked run's status carries its gate ask; only a running run surfaces its
    # latest agent call. So a parked board row never queries agent_calls — the
    # board runs this per subject.
    if run and not run.is_parked:
        return AgentCall.list_for_run(run.id)
    return []


def _status(driving_run: Run | None, calls: list[AgentCall]) -> SubjectStatus:
    # Facts only: the extension's UI renders its copy from them.
    if not driving_run:
        return SubjectStatus(state=RunState.SCHEDULED)
    # A parked run is always the active one (ACTIVE_STATES), so the
    # driving run alone decides parked-ness.
    parked = driving_run.state == RunState.PARKED.value
    # ``agent`` is the *running* run's latest agent — a parked run's calls are
    # history, not the current step, whichever caller handed them in. ``gate``
    # is the inverse: only a parked run's input_gate is a live ask (a timed-out
    # run keeps the stale column).
    agent = None
    if calls and not parked:
        agent = calls[-1].agent
    return SubjectStatus(
        state=RunState(driving_run.state),
        kind=driving_run.kind,
        agent=agent,
        gate=driving_run.input_gate if parked else None,
        failure=driving_run.failure,
        reason=driving_run.failure_code,
        triggered_at=driving_run.created_at,
        account_username=driving_run.account.username,
    )


def read_slice(path: Path, *, offset: int, limit: int) -> TextSlice:
    # A bounded byte window of a text file. A multibyte character split at a
    # window seam shows one � — the live tail below does the same. Negative
    # offset reads the tail; missing file is an empty eof slice.
    if not path.exists():
        return TextSlice(offset=0, next_offset=0, eof=True, has_earlier=False, text="")
    size = path.stat().st_size
    if offset < 0:
        offset = max(size + offset, 0)
    with path.open("rb") as handle:
        handle.seek(offset)
        payload = handle.read(limit)
    next_offset = offset + len(payload)
    return TextSlice(
        offset=offset,
        next_offset=next_offset,
        eof=next_offset >= size,
        has_earlier=offset > 0,
        text=payload.decode("utf-8", errors="replace"),
    )


def read_transcript_chunk(
    call: AgentCall,
    stream: Literal["stdout", "stderr"],
    *,
    offset: int,
    limit: int,
) -> TranscriptChunk:
    # One paginated slice of an agent call's stdout/stderr log — the initial
    # backfill before the live tail takes over. An empty eof chunk while the call
    # has produced no log yet. The platform owns the log path.
    path = call.get_stream_path(stream)
    if not path:
        return TranscriptChunk(
            call_id=call.id, stream=stream, offset=offset, next_offset=offset, eof=True, text=""
        )
    piece = read_slice(path, offset=offset, limit=limit)
    return TranscriptChunk(
        call_id=call.id,
        stream=stream,
        offset=piece.offset,
        next_offset=piece.next_offset,
        eof=piece.eof,
        text=piece.text,
    )


async def stream_transcript(
    engine: Engine,
    call_id: str,
    stream: Literal["stdout", "stderr"],
    *,
    offset: int = 0,
    poll_interval: float = _TRANSCRIPT_POLL_SECONDS,
    keepalive_interval: float = _TRANSCRIPT_KEEPALIVE_SECONDS,
) -> AsyncIterator[str]:
    # Tail an agent call's log from ``offset``, emitting each new slice as a
    # ``transcript.chunk`` and closing with ``agent_call.finished`` once the call reaches
    # a terminal state. The platform owns the call's log, so an extension supplies only the
    # call id — no resolver, no poll loop. Keepalive comments cover idle ticks. Each poll
    # re-derives the call's status off its run, so a call left unfinished when its run
    # went terminal reads "abandoned" and closes the stream on the next tick.
    elapsed = 0.0
    last_keepalive = 0.0
    while True:
        with session_scope(engine):
            call = AgentCall.get(call_id)
            path = call.get_stream_path(stream) if call else None
            summary = AgentCallResponse.model_validate(call) if call else None

        if not summary:
            # Unknown (or deleted) call: nothing will ever arrive — close the
            # stream instead of keepaliving forever.
            return

        if path and path.exists():
            size = path.stat().st_size
            if size > offset:
                with path.open("rb") as handle:
                    handle.seek(offset)
                    payload = handle.read(size - offset)
                if payload:
                    yield serialize_model_event(
                        "transcript.chunk",
                        TranscriptChunk(
                            call_id=call_id,
                            stream=stream,
                            offset=offset,
                            next_offset=offset + len(payload),
                            eof=False,
                            text=payload.decode("utf-8", errors="replace"),
                        ),
                    )
                    offset += len(payload)

        if summary.status in _TERMINAL_CALL_STATES:
            yield serialize_model_event("agent_call.finished", summary)
            return

        if elapsed - last_keepalive >= keepalive_interval:
            yield keepalive_comment()
            last_keepalive = elapsed

        try:
            await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            return
        elapsed += poll_interval
