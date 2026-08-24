from typing import Annotated

from fastapi import APIRouter, Depends, Path

from druks.accounts.dependencies import current_account
from druks.accounts.models import Account
from druks.api.exceptions import RunNotFound, agent_error_responses
from druks.mcp.gateway import exceptions as gate_errors
from druks.mcp.gateway import schemas, services

# Docstrings here are the derived tool descriptions and operation_id is the
# tool name — renaming one is a break, never a refactor side effect.
router = APIRouter(prefix="/api", tags=["agent"])


@router.get(
    "/gates/{run}",
    operation_id="get_gate",
    response_model=schemas.GateResponse,
    response_model_by_alias=True,
    responses=agent_error_responses(
        RunNotFound("run-123"),
        gate_errors.GateNotOpen("run-123"),
        gate_errors.GateNotAnswerable("run-123"),
    ),
)
async def get_gate(
    run: Annotated[str, Path(description="The parked run, from list_open_subjects.")],
) -> schemas.GateResponse:
    """A parked run's open gate: the ask, a bounded artifact chunk, and
    parkedAt — echo parkedAt unchanged to answer_gate."""
    return await services.get_gate(run)


@router.post(
    "/gates/{run}/answer",
    operation_id="answer_gate",
    openapi_extra={"x-destructive": False, "x-idempotent": True},
    response_model=schemas.GateAnswerResponse,
    response_model_by_alias=True,
    responses=agent_error_responses(
        gate_errors.InvalidGateAnswer("unknown control 'merge'"),
        RunNotFound("run-123"),
        gate_errors.GateRoundStale("run-123"),
        gate_errors.GateNotOpen("run-123"),
        gate_errors.GateNotAnswerable("run-123"),
    ),
)
async def answer_gate(
    run: Annotated[str, Path(description="The parked run from get_gate.")],
    body: schemas.AnswerGateRequest,
) -> schemas.GateAnswerResponse:
    """Answer the gate get_gate showed, resuming the run. parkedAt must echo
    get_gate's value unchanged; a repeat answer to the same parkedAt reports
    already_answered. Empty request_changes is valid only when get_gate's ask
    has non-blank context."""
    return await services.answer_gate(
        run,
        parked_at=body.parked_at,
        control=body.control,
        answers=body.answers,
        note=body.note,
    )


@router.get(
    "/agent-calls/{call}",
    operation_id="get_agent_call",
    response_model=schemas.AgentCallDetailResponse,
    response_model_by_alias=True,
    responses=agent_error_responses(gate_errors.AgentCallNotFound("call-123")),
)
async def get_agent_call(
    call: Annotated[str, Path(description="An agent call, latestAgentCall in list_open_subjects.")],
) -> schemas.AgentCallDetailResponse:
    """One agent call's metadata with bounded transcript and stderr tails and
    an artifact chunk."""
    return await services.get_agent_call(call)


@router.get(
    "/usage/summary",
    operation_id="get_usage",
    response_model=schemas.AgentUsageResponse,
    response_model_by_alias=True,
)
async def get_usage(account: Account = Depends(current_account)) -> schemas.AgentUsageResponse:
    """The caller's harness quota snapshot and today's spend. Pure read — it
    never triggers a scrape."""
    return await services.get_usage(account)
