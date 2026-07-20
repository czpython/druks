from typing import Annotated

from fastapi import APIRouter, Body, Depends

from druks.accounts.dependencies import current_account
from druks.accounts.models import Account
from druks.mcp.gateway import schemas, services

# Docstrings here are the derived tool descriptions and operation_id is the
# tool name — renaming one is a break, never a refactor side effect.
router = APIRouter(prefix="/api", tags=["agent"])


@router.get(
    "/gates/{run_id}",
    operation_id="get_gate",
    response_model=schemas.GateResponse,
    response_model_by_alias=True,
)
async def get_gate(run_id: str) -> schemas.GateResponse:
    """A parked run's open gate: the ask, a bounded artifact chunk, and
    parkedAt — echo parkedAt unchanged to answer_gate."""
    return services.get_gate(run_id)


@router.post(
    "/gates/{run_id}/answer",
    operation_id="answer_gate",
    response_model=schemas.GateAnswerResponse,
    response_model_by_alias=True,
)
async def answer_gate(run_id: str, body: schemas.AnswerGateRequest) -> schemas.GateAnswerResponse:
    """Answer the gate get_gate showed, resuming the run. parkedAt must echo
    get_gate's value unchanged; a repeat answer to the same parkedAt reports
    already_answered."""
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
    response_model=schemas.AgentCallDetailResponse,
    response_model_by_alias=True,
)
async def get_agent_call(call_id: str) -> schemas.AgentCallDetailResponse:
    """One agent call's metadata with bounded transcript and stderr tails and
    an artifact chunk."""
    return services.get_agent_call(call_id)


@router.post(
    "/runs/{run_id}/cancel",
    operation_id="cancel_run",
    response_model=schemas.CancelRunResponse,
    response_model_by_alias=True,
)
async def cancel_run(
    run_id: str, reason: Annotated[str, Body(embed=True, min_length=1, max_length=500)]
) -> schemas.CancelRunResponse:
    """Cancel an active run, recording the reason as its failure; a repeat
    cancel reports already_cancelled."""
    return await services.cancel_run(run_id, reason=reason)


@router.get(
    "/usage/summary",
    operation_id="get_usage",
    response_model=schemas.AgentUsageResponse,
    response_model_by_alias=True,
)
async def get_usage(account: Account = Depends(current_account)) -> schemas.AgentUsageResponse:
    """The caller's harness quota snapshot and today's spend. Pure read — it
    never triggers a scrape."""
    return services.get_usage(account)
