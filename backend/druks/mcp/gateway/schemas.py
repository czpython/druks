from datetime import datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from druks.durable.schemas import AgentCallSummary
from druks.schemas import BaseResponse
from druks.usage.schemas import UsageHistoryPoint


class ArtifactContent(BaseResponse):
    call_id: str
    kind: str
    title: str
    content: str


class GateDetail(BaseResponse):
    # Everything needed to answer a parked run in one read: the ask, the
    # artifact under review, the reply's JSON Schema, and parked_at — the park
    # identity answer_gate must echo back.
    run_id: str
    gate: str
    parked_at: datetime
    ask: dict[str, Any]
    artifact: ArtifactContent | None = None
    reply_schema: dict[str, Any]


class GateAnswerResult(BaseResponse):
    run_id: str
    parked_at: datetime
    result: Literal["answered", "already_answered"]


class AnswerGateRequest(BaseModel):
    # parkedAt echoes get_gate's response key unchanged — the park identity the
    # answer must land on; one camelCase wire both directions. The rest mirrors
    # ResumeRequest: a control the ask offered, an answer per open question, an
    # optional free-text note.
    model_config = ConfigDict(str_strip_whitespace=True, alias_generator=to_camel)
    parked_at: AwareDatetime
    control: str
    answers: dict[str, str] = Field(default_factory=dict)
    note: str = ""


class AgentCallDetail(BaseResponse):
    run_id: str
    call: AgentCallSummary
    transcript: str
    stderr: str
    artifact: ArtifactContent | None = None


class CancelRunResult(BaseResponse):
    run_id: str
    result: Literal["cancelled", "already_cancelled"]


class AgentHarnessUsage(BaseResponse):
    # One harness's quota for the agent surface: the latest snapshot's facts
    # plus a short percent-left trend per window, oldest first.
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


class AgentUsage(BaseResponse):
    # The caller's spend for the operator-local day plus per-harness quota.
    day: str
    timezone: str
    spend_today_usd: float
    tokens_today: int
    runs_today: int
    harnesses: list[AgentHarnessUsage] = Field(default_factory=list)
