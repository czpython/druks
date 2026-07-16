# The durable read side: every query that composes rows into response shapes.
# Schemas stay pure projections; routes call in here.
import asyncio
from collections.abc import AsyncIterator
from typing import Literal

from sqlalchemy import Engine

from druks.accounts.models import Account
from druks.database import session_scope
from druks.durable.live import keepalive_comment, serialize_model_event

from .enums import RunState
from .exceptions import GateTimeout
from .models import AgentCall, Artifact, Run
from .schemas import (
    AgentCallFiles,
    AgentCallResponse,
    RunResponse,
    SubjectActivity,
    SubjectResponse,
    SubjectStatus,
    SubjectSummary,
    TranscriptChunk,
    get_display_label,
)

_TRANSCRIPT_POLL_SECONDS = 0.5
_TRANSCRIPT_KEEPALIVE_SECONDS = 15.0
_TERMINAL_CALL_STATES = {"succeeded", "failed", "abandoned"}


def get_agent_call_files(call_id: str) -> AgentCallFiles | None:
    call = AgentCall.get(call_id)
    if not call:
        return None
    return AgentCallFiles.from_call(call, Artifact.get_for_call(call.id))


def list_subject_timeline(subject_type: str, subject_id: str) -> list[RunResponse]:
    # The subject's whole timeline: every run about it, oldest first, each
    # with its agent calls.
    runs = Run.list_for_subject(subject_type, subject_id)
    return _timeline(runs, AgentCall.by_run([run.id for run in runs]))


def get_subject_status(subject_type: str, subject_id: str) -> SubjectStatus:
    runs = Run.list_for_subject(subject_type, subject_id)
    active_run = next((run for run in runs if run.is_active), None)
    return _status(runs, active_run, _running_calls(active_run))


def get_subject_response(
    subject_type: str,
    subject_id: str,
    *,
    summary: SubjectSummary,
    activity: SubjectActivity | None = None,
) -> SubjectResponse:
    # One fetch feeds both the status derivation and the timeline.
    runs = Run.list_for_subject(subject_type, subject_id)
    calls_by_run = AgentCall.by_run([run.id for run in runs])
    active_run = next((run for run in runs if run.is_active), None)
    active_calls: list[AgentCall] = []
    if active_run:
        active_calls = calls_by_run[active_run.id]
    return SubjectResponse(
        summary=summary,
        status=_status(runs, active_run, active_calls),
        timeline=_timeline(runs, calls_by_run),
        activity=activity,
    )


def _timeline(runs: list[Run], calls_by_run: dict[str, list[AgentCall]]) -> list[RunResponse]:
    # by_run keys every run id, so the lookup is total. The responses display
    # account emails; the rows store ids — one lookup covers the timeline.
    ordered = sorted(runs, key=lambda run: (run.created_at, run.id))
    attributed = {run.account_id for run in runs if run.account_id}
    attributed |= {
        call.account_id for calls in calls_by_run.values() for call in calls if call.account_id
    }
    emails = Account.emails_by_id(attributed)
    return [RunResponse.from_run(run, calls_by_run[run.id], emails) for run in ordered]


def _running_calls(active_run: Run | None) -> list[AgentCall]:
    # A parked run's label is its gate ask; only a running run's reads its latest
    # agent call. So a parked board row never queries agent_calls — the board runs
    # this per subject.
    if active_run and active_run.state != RunState.PENDING_INPUT.value:
        return AgentCall.list_for_run(active_run.id)
    return []


def _status(
    runs: list[Run], active_run: Run | None, active_calls: list[AgentCall]
) -> SubjectStatus:
    # The newest active run drives the subject — a stale parked run a fresh dispatch
    # superseded must not outrank it; once all are terminal, the latest one's outcome
    # stands. State and its failure ride that run; the label is its own derivation.
    driving_run = active_run or (runs[0] if runs else None)
    return SubjectStatus(
        state=RunState(driving_run.state) if driving_run else RunState.SCHEDULED,
        label=_subject_label(active_run, runs, active_calls),
        failure=driving_run.failure if driving_run else None,
    )


def _subject_label(active_run: Run | None, runs: list[Run], active_calls: list[AgentCall]) -> str:
    # The one-line "what now": a parked run's ask label, a timed-out gate's retry
    # hint, else the active run's latest agent call (or its kind before any call).
    if not active_run:
        newest_run = runs[0] if runs else None
        if newest_run and newest_run.failure_code == GateTimeout.code:
            # An unanswered gate, not a crash — and the run is terminal, so a
            # fresh trigger goes straight through; say so instead of a bare
            # "failed" the operator would read as broken.
            return f"{get_display_label(newest_run.kind)} timed out — re-trigger to retry"
        return ""
    if active_run.state == RunState.PENDING_INPUT.value:
        ask = active_run.get_ask() if active_run.input_request else {}
        return ask.get("label") or "Waiting on you"
    if active_calls and active_calls[-1].agent:
        return get_display_label(active_calls[-1].agent)
    return get_display_label(active_run.kind)


def read_transcript_chunk(
    engine: Engine,
    call_id: str,
    stream: Literal["stdout", "stderr"],
    *,
    offset: int,
    limit: int,
) -> TranscriptChunk | None:
    # One paginated slice of an agent call's stdout/stderr log — the initial
    # backfill before the live tail takes over. None if the call is gone; an empty
    # eof chunk if it has produced no log yet. The platform owns the log path.
    with session_scope(engine):
        call = AgentCall.get(call_id)
        if not call:
            return None
        path = call.get_stream_path(stream)
    if not path or not path.exists():
        return TranscriptChunk(
            call_id=call_id, stream=stream, offset=offset, next_offset=offset, eof=True, text=""
        )
    with path.open("rb") as handle:
        handle.seek(offset)
        payload = handle.read(limit)
    next_offset = offset + len(payload)
    return TranscriptChunk(
        call_id=call_id,
        stream=stream,
        offset=offset,
        next_offset=next_offset,
        eof=next_offset >= path.stat().st_size,
        text=payload.decode("utf-8", errors="replace"),
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
            summary = AgentCallResponse.from_call(call) if call else None

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
