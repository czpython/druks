from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

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


class AgentCallResponse(BaseResponse):
    id: str
    # Which agent made this call ("scope", "implement") — the timeline's row label.
    agent: str | None = None
    label: str = ""
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
        # Liveness is derived, not stored: an unfinished call reads "running" while its
        # run is active and "abandoned" once the run is terminal (the run ended without
        # the call closing). A finished call keeps its recorded outcome.
        if call.finished_at:
            status = AgentCallStatus(call.status).value
        elif call.run.is_active:
            status = "running"
        else:
            status = "abandoned"
        return cls(
            id=call.id,
            agent=call.agent,
            label=get_display_label(call.agent) if call.agent else "Agent",
            status=status,  # type: ignore[arg-type]
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
    url: str


class ArtifactDescriptor(BaseResponse):
    # A call's renderable output (a plan's markdown), rendered by kind — distinct
    # from the raw files. Content lives in the call dir, served by the same route.
    kind: str
    title: str
    url: str

    @classmethod
    def from_artifact(cls, artifact: Artifact, *, base: str) -> "ArtifactDescriptor":
        return cls(
            kind=artifact.kind, title=artifact.title, url=base + quote(artifact.path, safe="")
        )


class AgentCallFiles(BaseResponse):
    # A call's on-disk artifacts, each with a download URL back to the transcript router.
    # The slot the file occupies is its role (prompt / response / stdout / stderr /
    # metadata / manifest).
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
        base = f"/api/{call.run.extension}/transcripts/{call.id}/files/"

        def named(path: Path) -> ArtifactFile | None:
            if not path.is_file():
                return None
            stat = path.stat()
            return ArtifactFile(
                name=path.name,
                size_bytes=stat.st_size,
                updated_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                url=base + quote(path.name, safe=""),
            )

        descriptor = ArtifactDescriptor.from_artifact(artifact, base=base) if artifact else None
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
            agent_calls=[AgentCallResponse.from_call(c) for c in calls],
        )


class SubjectSummary(BaseResponse):
    # The base an extension's subject header subclasses; ``id`` keys the subject's
    # status, timeline, and detail URL, and the extension adds its own domain fields.
    id: str


class SubjectStatus(BaseResponse):
    # The subject's lifecycle status for the dashboard lane — derived by the read
    # side, never stored; ``state`` is the canonical RunState aggregated across
    # the subject's runs.
    state: RunState
    label: str
    # The stop reason of the run driving ``state`` — set only when that run is
    # terminal-failed (an active or finished subject carries none). Lets a board
    # render "why" inline without reaching into the timeline.
    failure: str | None = None


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
