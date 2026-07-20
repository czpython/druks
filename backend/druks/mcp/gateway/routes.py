from typing import Annotated

from fastapi import APIRouter, Body, Depends

from druks.accounts.dependencies import current_account
from druks.accounts.models import Account
from druks.mcp.gateway import services
from druks.mcp.gateway.schemas import (
    AgentCallDetail,
    AgentUsage,
    AnswerGateRequest,
    CancelRunResult,
    GateAnswerResult,
    GateDetail,
)

router = APIRouter(prefix="/mcp", tags=["mcp"])


@router.get(
    "/gates/{run_id}",
    operation_id="get_gate",
    response_model=GateDetail,
    response_model_by_alias=True,
)
async def get_gate(run_id: str) -> GateDetail:
    return services.get_gate(run_id)


@router.post(
    "/gates/{run_id}/answer",
    operation_id="answer_gate",
    response_model=GateAnswerResult,
    response_model_by_alias=True,
)
async def answer_gate(run_id: str, body: AnswerGateRequest) -> GateAnswerResult:
    return await services.answer_gate(
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
    return services.get_agent_call(call_id)


@router.post(
    "/runs/{run_id}/cancel",
    operation_id="cancel_run",
    response_model=CancelRunResult,
    response_model_by_alias=True,
)
async def cancel_run(
    run_id: str, reason: Annotated[str, Body(embed=True, min_length=1, max_length=500)]
) -> CancelRunResult:
    return await services.cancel_run(run_id, reason=reason)


@router.get(
    "/usage",
    operation_id="get_usage",
    response_model=AgentUsage,
    response_model_by_alias=True,
)
async def get_usage(account: Account = Depends(current_account)) -> AgentUsage:
    return services.get_usage(account)
