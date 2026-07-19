from typing import Annotated

from fastapi import APIRouter, Body
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from druks.durable import agent
from druks.durable.schemas import AgentCallDetail, CancelRunResult, GateAnswerResult, GateView

router = APIRouter(prefix="/api/agent", tags=["agent"])


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


@router.get(
    "/gates/{run_id}",
    operation_id="get_gate",
    response_model=GateView,
    response_model_by_alias=True,
)
async def get_gate(run_id: str) -> GateView:
    return agent.get_gate(run_id)


@router.post(
    "/gates/{run_id}/answer",
    operation_id="answer_gate",
    response_model=GateAnswerResult,
    response_model_by_alias=True,
)
async def answer_gate(run_id: str, body: AnswerGateRequest) -> GateAnswerResult:
    return await agent.answer_gate(
        run_id,
        parked_at=body.parked_at,
        control=body.control,
        answers=body.answers,
        note=body.note,
    )


@router.get(
    "/agent-calls/{call_id}",
    operation_id="get_agent_call",
    response_model=AgentCallDetail,
    response_model_by_alias=True,
)
async def get_agent_call(call_id: str) -> AgentCallDetail:
    return agent.get_agent_call(call_id)


@router.post(
    "/runs/{run_id}/cancel",
    operation_id="cancel_run",
    response_model=CancelRunResult,
    response_model_by_alias=True,
)
async def cancel_run(
    run_id: str, reason: Annotated[str, Body(embed=True, min_length=1, max_length=500)]
) -> CancelRunResult:
    return await agent.cancel_run(run_id, reason=reason)
