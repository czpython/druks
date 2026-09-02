from datetime import datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from druks.durable.schemas import AgentCallResponse
from druks.schemas import Schema
from druks.usage.schemas import UsageHistoryPoint


class ArtifactContent(Schema):
    call_id: str
    kind: str
    title: str
    content: str


class GateResponse(Schema):
    # parked_at is the park identity answer_gate must echo back.
    run: str
    gate: str
    parked_at: datetime
    ask: dict[str, Any]
    artifact: ArtifactContent | None = None


class GateAnswerResponse(Schema):
    run: str
    parked_at: datetime
    result: Literal["answered", "already_answered"]


class AnswerGateRequest(BaseModel):
    # parked_at echoes get_gate's value unchanged.
    model_config = ConfigDict(str_strip_whitespace=True, alias_generator=to_camel)
    parked_at: AwareDatetime = Field(
        description="When the run parked, echoed from get_gate unchanged — it names the "
        "exact question being answered."
    )
    control: str = Field(
        description="The decision to take: one of the ids the ask offers as controls, e.g. approve."
    )
    answers: dict[str, str] = Field(
        default_factory=dict,
        description="One answer per ask question, keyed by the question's id.",
    )
    note: str = Field(default="", description="The optional note to submit with this answer.")


class AgentCallDetailResponse(Schema):
    run: str
    call: AgentCallResponse
    transcript: str
    stderr: str
    artifact: ArtifactContent | None = None


class AgentProviderUsage(Schema):
    # *_history: percent-left trend samples, oldest first.
    id: str
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


class AgentUsageResponse(Schema):
    day: str
    timezone: str
    spend_today_usd: float
    tokens_today: int
    runs_today: int
    providers: list[AgentProviderUsage] = Field(default_factory=list)
