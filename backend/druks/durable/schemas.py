from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SerializeAsAny

from druks.harnesses.artifacts import normalize_token_usage
from druks.schemas import BaseResponse

from .enums import AgentCallStatus, RunState
from .models import AgentCall, Artifact, Run


class TokenUsage(BaseResponse):
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    cache_creation_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int


def _token_usage(cost_metadata: dict | None) -> TokenUsage | None:
    canonical = normalize_token_usage(cost_metadata)
    return TokenUsage(**canonical) if canonical else None


def get_display_label(kind: str) -> str:
    # "build.build_workflow" → "Build workflow"; "implement" → "Implement".
    return kind.rsplit(".", 1)[-1].replace("_", " ").capitalize()


def _derived_status(call: AgentCall) -> str:
    # Liveness is derived, not stored: an unfinished call reads "running" while its
    # run is active and "abandoned" once the run is terminal (the run ended without
    # the call closing). A finished call keeps its recorded outcome.
    if call.finished_at:
        return AgentCallStatus(call.status).value
    if call.run.is_active:
        return "running"
    return "abandoned"


class AgentCallResponse(BaseResponse):
    id: str
    # Which agent made this call ("scope", "implement") — the timeline's row label.
    agent: str | None = None
    label: str = ""
    # The account charged — differs from the run's on fallback.
    account_username: str
    status: Literal["running", "succeeded", "failed", "abandoned"]
    # started_at + finished_at are the facts; the client derives elapsed (a live
    # tick off started_at while running), so nothing here churns between polls.
    started_at: datetime
    finished_at: datetime | None = None
    last_error: str | None = None
    cost_usd: float | None = None
    tokens: TokenUsage | None = None

    @classmethod
    def from_call(cls, call: AgentCall) -> "AgentCallResponse":
        return cls(
            id=call.id,
            agent=call.agent,
            label=get_display_label(call.agent) if call.agent else "Agent",
            account_username=call.account.username,
            status=_derived_status(call),  # type: ignore[arg-type]
            started_at=call.started_at,
            finished_at=call.finished_at,
            last_error=call.last_error,
            cost_usd=call.cost_usd,
            tokens=_token_usage(call.cost_metadata),
        )


class ArtifactFile(BaseResponse):
    name: str
    size_bytes: int
    updated_at: datetime


class ArtifactDescriptor(BaseResponse):
    # A call's renderable output (a plan's markdown), rendered by kind — distinct
    # from the raw files. ``name`` is its file in the call dir, downloadable from
    # the transcript files route like any other.
    kind: str
    title: str
    name: str


class AgentCallFiles(BaseResponse):
    # A call's on-disk artifacts by role (prompt / response / stdout / stderr /
    # metadata / manifest). Each carries its file name; the client composes the
    # download URL from the transcript route it fetched this listing from.
    prompt: ArtifactFile | None = None
    stdout: ArtifactFile | None = None
    stderr: ArtifactFile | None = None
    response: ArtifactFile | None = None
    metadata: ArtifactFile | None = None
    # The capability manifest for the call: model, harness, MCP availability,
    # enabled skills — presence only, never a secret value.
    manifest: ArtifactFile | None = None
    # The call's renderable output, rendered by kind; None unless it produced one.
    artifact: ArtifactDescriptor | None = None

    @classmethod
    def from_call(cls, call: AgentCall, artifact: Artifact | None) -> "AgentCallFiles":
        layout = call.artifact_layout

        def named(path: Path) -> ArtifactFile | None:
            if not path.is_file():
                return
            stat = path.stat()
            return ArtifactFile(
                name=path.name,
                size_bytes=stat.st_size,
                updated_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
            )

        descriptor = None
        if artifact:
            descriptor = ArtifactDescriptor(
                kind=artifact.kind, title=artifact.title, name=artifact.path
            )
        return cls(
            prompt=named(layout.prompt),
            response=named(layout.output),
            metadata=named(layout.metadata),
            manifest=named(layout.manifest),
            stdout=named(layout.transcript),
            stderr=named(layout.stderr),
            artifact=descriptor,
        )


class RunResponse(BaseResponse):
    id: str
    # The durable kind ("build.scope"); ``label`` is its display name ("Scope").
    kind: str
    label: str
    state: Literal["scheduled", "running", "pending_input", "finished", "failed", "cancelled"]
    failure: str | None = None
    # The structured ask the parked run declared at ``Gate.wait(input_request=…)`` —
    # set while the run is PENDING_INPUT, cleared on resume. Presence = "needs you".
    input_request: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    # Who asked; "system" when nobody did.
    account_username: str
    agent_calls: list[AgentCallResponse] = Field(default_factory=list)

    @classmethod
    def from_run(cls, run: Run, calls: list[AgentCall]) -> "RunResponse":
        return cls(
            id=run.id,
            kind=run.kind,
            label=get_display_label(run.kind),
            state=run.state,  # type: ignore[arg-type]
            failure=run.failure,
            input_request=run.get_ask() if run.input_request else None,
            created_at=run.created_at,
            updated_at=run.updated_at,
            account_username=run.account.username,
            agent_calls=[AgentCallResponse.from_call(c) for c in calls],
        )


class SubjectSummary(BaseResponse):
    # The base an extension's subject header subclasses; ``id`` keys the subject's
    # status, timeline, and detail URL, and the extension adds its own domain fields.
    id: str


class SubjectStatus(BaseResponse):
    # The subject's lifecycle status for the dashboard lane — derived by the read
    # side, never stored; ``state`` is the canonical RunState aggregated across
    # the subject's runs. Everything else is a fact the extension's UI renders
    # its own copy from; the platform ships no prose.
    state: RunState
    # The driving run's kind and, while running, its latest agent call's agent.
    kind: str | None = None
    agent: str | None = None
    # A parked run's gate identity — the extension's UI maps it to its own
    # words; the ask's content rides the timeline's ``input_request``.
    gate: str | None = None
    # The stop reason of the run driving ``state`` — set only when that run is
    # terminal-failed (an active or finished subject carries none). Lets a board
    # render "why" inline without reaching into the timeline. ``reason`` is its
    # machine-readable classification (``gate_timeout``): an unanswered gate,
    # not a crash.
    failure: str | None = None
    reason: str | None = None


class SubjectRow(BaseResponse):
    summary: SerializeAsAny[SubjectSummary]
    status: SubjectStatus


class SubjectList(BaseResponse):
    rows: list[SubjectRow] = Field(default_factory=list)


class SubjectActivity(BaseResponse):
    # The running sub-phase the timeline can't show ("Building sandbox VM…"), supplied
    # by the extension; ``kind`` groups it for display ("infra" | "agent").
    label: str
    kind: str


class SubjectResponse(BaseResponse):
    summary: SerializeAsAny[SubjectSummary]
    status: SubjectStatus
    # The subject's runs, oldest first, each with its agent calls — the timeline.
    timeline: list[RunResponse] = Field(default_factory=list)
    activity: SubjectActivity | None = None


class TranscriptChunk(BaseResponse):
    call_id: str
    stream: Literal["stdout", "stderr"]
    offset: int
    next_offset: int
    eof: bool
    text: str


class TextSlice(BaseResponse):
    # One bounded UTF-8-safe cut of an on-disk text file; offsets are byte
    # positions, has_earlier marks content before this slice.
    offset: int
    next_offset: int
    eof: bool
    has_earlier: bool
    text: str


class AgentCallSummary(BaseResponse):
    # The bounded agent-surface cut of AgentCallResponse: the same facts with
    # clipped free text and no token breakdown.
    id: str
    agent: str | None = None
    account_username: str
    status: Literal["running", "succeeded", "failed", "abandoned"]
    started_at: datetime
    finished_at: datetime | None = None
    last_error: str | None = None
    cost_usd: float | None = None

    @classmethod
    def from_call(cls, call: AgentCall) -> "AgentCallSummary":
        return cls(
            id=call.id,
            agent=call.agent,
            account_username=call.account.username,
            status=_derived_status(call),  # type: ignore[arg-type]
            started_at=call.started_at,
            finished_at=call.finished_at,
            last_error=call.last_error,
            cost_usd=call.cost_usd,
        )
