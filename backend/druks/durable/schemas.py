from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import (
    AliasPath,
    BeforeValidator,
    ConfigDict,
    Field,
    SerializeAsAny,
    StringConstraints,
    computed_field,
)

from druks.schemas import BaseResponse

from .enums import AgentCallStatus, RunState

if TYPE_CHECKING:
    from .models import AgentCall, Run


class TokenUsage(BaseResponse):
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    cache_creation_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int


def get_display_label(kind: str) -> str:
    # "ship.build" → "Build"; "implement" → "Implement".
    return kind.rsplit(".", 1)[-1].replace("_", " ").capitalize()


class AgentCallResponse(BaseResponse):
    model_config = ConfigDict(from_attributes=True)

    id: str
    # Which agent made this call ("scope", "implement") — the timeline's row label.
    agent: str
    # The account charged — differs from the run's on fallback.
    account_username: str = Field(validation_alias=AliasPath("account", "username"))
    status: AgentCallStatus = Field(validation_alias="live_status")
    # started_at + finished_at are the facts; the client derives elapsed (a live
    # tick off started_at while running), so nothing here churns between polls.
    started_at: datetime
    finished_at: datetime | None = None
    last_error: str | None = None
    cost_usd: float | None = None
    tokens: TokenUsage | None = Field(default=None, validation_alias="token_usage")

    @computed_field
    @property
    def label(self) -> str:
        return get_display_label(self.agent)


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
    # delivered skills — presence only, never a secret value.
    manifest: ArtifactFile | None = None
    # The call's renderable output, rendered by kind; None unless it produced one.
    artifact: ArtifactDescriptor | None = None


class RunResponse(BaseResponse):
    id: str
    # The durable kind ("ship.build"); ``label`` is its display name ("Build").
    kind: str
    label: str
    state: Literal["scheduled", "running", "parked", "finished", "failed", "cancelled"]
    failure: str | None = None
    gate: str | None = None
    # The structured ask the parked run declared at ``Gate.wait(input_request=…)`` —
    # set while the run is PARKED, cleared on resume. Presence = "needs you".
    input_request: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    # Who asked; "system" when nobody did.
    account_username: str
    agent_calls: list[AgentCallResponse] = Field(default_factory=list)

    @classmethod
    def from_run(
        cls,
        run: "Run",
        calls: list["AgentCall"],
        *,
        input_request: dict[str, Any] | None,
    ) -> "RunResponse":
        return cls(
            id=run.id,
            kind=run.kind,
            label=get_display_label(run.kind),
            state=run.state,  # type: ignore[arg-type]
            failure=run.failure,
            gate=run.input_gate,
            input_request=input_request,
            created_at=run.created_at,
            updated_at=run.updated_at,
            account_username=run.account.username,
            agent_calls=[AgentCallResponse.model_validate(call) for call in calls],
        )


# A subject id is a string wherever it is read — the URL segment, the DBOS
# attribute, the dedup key — while the row behind one is keyed by an integer. So it
# takes either and is always a string.
SubjectId = Annotated[str, BeforeValidator(str)]

# The one line a board row and a detail page show a subject as; blank is
# rejected rather than rendered.
SubjectLabel = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class SubjectSummary(BaseResponse):
    # The base an extension's subject header subclasses; ``id`` keys the subject's
    # status, timeline and detail URL, and ``from_attributes`` builds the header
    # straight off the subject.
    model_config = ConfigDict(from_attributes=True)

    id: SubjectId
    label: SubjectLabel


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
    # When druks last picked this subject up, and whose work it is. A subject with
    # a row of its own keeps these in its own columns; one that is identity alone
    # has only the driving run, which is what everything above already comes from.
    triggered_at: datetime | None = None
    account_username: str | None = None

    @property
    def is_parked(self) -> bool:
        return self.state == RunState.PARKED

    @property
    def is_running(self) -> bool:
        return self.state == RunState.RUNNING

    @property
    def is_failed(self) -> bool:
        return self.state == RunState.FAILED


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
