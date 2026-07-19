# The durable half of the agent surface: gate read/answer, bounded call
# detail, and run cancel — shared by the /api/agent routes and the MCP tools.
from datetime import datetime
from typing import Any

from druks.database import db_session
from druks.durable.enums import RunState
from druks.durable.exceptions import (
    AgentCallNotFound,
    GateNotAnswerable,
    GateNotOpen,
    GateRoundStale,
    InvalidGateAnswer,
    RunNotActive,
    RunNotFound,
)
from druks.durable.models import AgentCall, Artifact, Run
from druks.durable.reads import read_slice
from druks.durable.schemas import (
    AgentCallDetail,
    AgentCallSummary,
    ArtifactChunk,
    CancelRunResult,
    GateAnswerResult,
    GateView,
    clip,
)
from druks.notifications.exceptions import InvalidChoiceError
from druks.notifications.services import validate_in_app_answer

_TRANSCRIPT_TAIL_BYTES = 8 * 1024
_STDERR_TAIL_BYTES = 4 * 1024
_ARTIFACT_CHUNK_BYTES = 4 * 1024
_NOTE_BYTES = 2 * 1024
_PROMPT_CLIP = 2 * 1024
_OPTION_LABEL_CLIP = 256


def _bounded_ask(ask: dict[str, Any]) -> dict[str, Any]:
    # The ask is agent-authored free text, so the view bounds each field like
    # every other budgeted response. Question COUNT stays whole — dropping a
    # question would make the gate unanswerable; real asks hold a handful.
    questions = [
        {
            **question,
            "prompt": clip(question.get("prompt"), _PROMPT_CLIP),
            "options": [
                {**option, "label": clip(option.get("label"), _OPTION_LABEL_CLIP)}
                for option in question.get("options", [])
            ],
        }
        for question in ask.get("questions", [])
    ]
    return {**ask, "questions": questions}


def get_gate(run_id: str) -> GateView:
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
    ask = _bounded_ask(run.get_ask())
    return GateView(
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
    if run.answered_parked_at == parked_at:
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
