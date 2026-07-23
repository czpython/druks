from datetime import datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from druks.durable.schemas import AgentCallResponse
from druks.schemas import BaseResponse
from druks.usage.schemas import UsageHistoryPoint


class ArtifactContent(BaseResponse):
    call_id: str
    kind: str
    title: str
    content: str


class GateResponse(BaseResponse):
    # parked_at is the park identity answer_gate must echo back.
    run_id: str
    gate: str
    parked_at: datetime
    ask: dict[str, Any]
    artifact: ArtifactContent | None = None


class GateAnswerResponse(BaseResponse):
    run_id: str
    parked_at: datetime
    result: Literal["answered", "already_answered"]


class AnswerGateRequest(BaseModel):
    # parked_at echoes get_gate's value unchanged.
    model_config = ConfigDict(str_strip_whitespace=True, alias_generator=to_camel)
    parked_at: AwareDatetime
    control: str
    answers: dict[str, str] = Field(default_factory=dict)
    note: str = ""


class AgentCallDetailResponse(BaseResponse):
    run_id: str
    call: AgentCallResponse
    transcript: str
    stderr: str
    artifact: ArtifactContent | None = None


class CancelRunResponse(BaseResponse):
    run_id: str
    result: Literal["cancelled", "already_cancelled"]


class AgentHarnessUsage(BaseResponse):
    # *_history: percent-left trend samples, oldest first.
    name: str
    is_connected: bool = False
    plan_tier: str | None = None
    five_hour_percent_left: int | None = None
    five_hour_resets_at: datetime | None = None
    week_percent_left: int | None = None
    week_resets_at: datetime | None = None
    is_unlimited: bool = False
    scraped_at: datetime | None = None
    five_hour_history: list[UsageHistoryPoint] = Field(default_factory=list)
    week_history: list[UsageHistoryPoint] = Field(default_factory=list)


class AgentUsageResponse(BaseResponse):
    day: str
    timezone: str
    spend_today_usd: float
    tokens_today: int
    runs_today: int
    harnesses: list[AgentHarnessUsage] = Field(default_factory=list)
