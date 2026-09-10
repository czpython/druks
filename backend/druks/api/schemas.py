from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from druks.durable.enums import RunState
from druks.schemas import Schema


class ResumeRequest(BaseModel):
    # The operator's decision on a parked run: a control id the ask offered, an answer
    # per question (an offered option id or the operator's own words), and an optional
    # free-text note. The control is checked against the stored ask; answers and note
    # are content for the next agent prompt, never control flow.
    model_config = ConfigDict(str_strip_whitespace=True)
    control: str
    answers: dict[str, str] = Field(default_factory=dict)
    note: str = ""


class CancelRunResponse(Schema):
    run: str
    result: Literal["cancelled", "already_cancelled"]


class RetryRunResponse(Schema):
    run: str


class OpenWorkflowResponse(Schema):
    app: str
    state: RunState
    run: str = Field(description="What get_gate, cancel_run, and retry_run take.")
    latest_agent_call: str | None = Field(
        description="What get_agent_call takes; null before the first call."
    )
    failure: str | None
    created_at: datetime


class OpenSubjectResponse(Schema):
    subject_type: str
    subject_id: str
    subject_label: str
    workflows: list[OpenWorkflowResponse]


class OpenSubjectsResponse(Schema):
    subjects: list[OpenSubjectResponse]


class DashboardRun(Schema):
    app: str
    run: str
    kind: str
    state: RunState
    subject_type: str | None
    subject_id: str | None
    subject_label: str | None
    updated_at: datetime
    parked_at: datetime | None
    request_label: str | None
    artifact_title: str | None
    presentation: str | None
    request_url: str | None
    failure: str | None


class DashboardSection(Schema):
    total: int
    rows: list[DashboardRun]


class DashboardOverview(Schema):
    needs_you: DashboardSection
    running: DashboardSection
    failed: DashboardSection
    last_finished_at: datetime | None
    last_failed_at: datetime | None


class DashboardSchedule(Schema):
    app: str
    kind: str
    cron: str | None
    default_cron: str
    enabled: bool
    timezone: str


class DashboardSchedules(Schema):
    rows: list[DashboardSchedule]


class ArtifactContent(Schema):
    # A call's renderable output, served to the in-app review so it can show the
    # plan (or other markdown) beside its controls.
    kind: str
    title: str
    content: str
