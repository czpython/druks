from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from druks.durable.enums import RunState
from druks.schemas import BaseResponse


class ResumeRequest(BaseModel):
    # The operator's decision on a parked run: a control id the ask offered, an answer
    # per question (an offered option id or the operator's own words), and an optional
    # free-text note. The control is checked against the stored ask; answers and note
    # are content for the next agent prompt, never control flow.
    model_config = ConfigDict(str_strip_whitespace=True)
    control: str
    answers: dict[str, str] = Field(default_factory=dict)
    note: str = ""


class CancelRunResponse(BaseResponse):
    run: str
    result: Literal["cancelled", "already_cancelled"]


class RetryRunResponse(BaseResponse):
    run: str


class OpenWorkflowResponse(BaseResponse):
    extension: str
    state: RunState
    run: str = Field(description="What get_gate, cancel_run, and retry_run take.")
    latest_agent_call: str | None = Field(
        description="What get_agent_call takes; null before the first call."
    )
    failure: str | None
    created_at: datetime


class OpenSubjectResponse(BaseResponse):
    subject_type: str
    subject_id: str
    subject_label: str
    workflows: list[OpenWorkflowResponse]


class OpenSubjectsResponse(BaseResponse):
    subjects: list[OpenSubjectResponse]


class ArtifactContent(BaseResponse):
    # A call's renderable output, served to the in-app review so it can show the
    # plan (or other markdown) beside its controls.
    kind: str
    title: str
    content: str


class WebhookSource(BaseResponse):
    source: str
    last_at: datetime | None = None


class WebhookFreshness(BaseResponse):
    # One entry per monitored webhook source, with its newest delivery timestamp;
    # the strip labels a tile per source.
    sources: list[WebhookSource] = Field(default_factory=list)


class DashboardHealth(BaseResponse):
    web: Literal["ok", "degraded"]
    webhook_freshness: WebhookFreshness
    spend_today_usd: float | None
    tokens_today: int
