from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, status

from druks.api.exceptions import RunNotActive, RunNotFailed, RunNotFound, SubjectBusy
from druks.api.schemas import CancelRunResponse, ResumeRequest, RetryRunResponse
from druks.durable.enums import RunState
from druks.durable.models import Run
from druks.notifications.exceptions import InvalidChoiceError
from druks.notifications.services import validate_in_app_answer

router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.post("/{run_id}/resume", status_code=status.HTTP_204_NO_CONTENT)
async def resume_run(run_id: str, body: ResumeRequest) -> None:
    # The in-app half of a gate: the operator answers the parked run from Druks
    # (external gates resume through their own webhook).
    run = Run.get(run_id)
    if not run:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    ask = run.input_request
    if run.state != RunState.PARKED.value or not ask:
        raise HTTPException(status.HTTP_409_CONFLICT, "run is not waiting on an in-app decision")
    try:
        resume_payload = validate_in_app_answer(ask, body.control, body.answers, body.note)
    except InvalidChoiceError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    await run.resume(**resume_payload)


# The "agent" tag also derives each route below into an MCP tool: its docstring
# is the tool description and operation_id is the tool name — renaming one is a
# break, never a refactor side effect.


@router.post(
    "/{run_id}/cancel",
    operation_id="cancel_run",
    tags=["agent"],
    openapi_extra={"x-idempotent": True},
    response_model=CancelRunResponse,
    response_model_by_alias=True,
)
async def cancel_run(
    run_id: str, reason: Annotated[str, Body(embed=True, min_length=1, max_length=500)]
) -> CancelRunResponse:
    """Cancel an active run, recording the reason as its failure; a repeat
    cancel reports already_cancelled."""
    run = Run.get(run_id)
    if not run:
        raise RunNotFound(run_id)
    if run.state == RunState.CANCELLED.value:
        return CancelRunResponse(run=run.id, result="already_cancelled")
    if not run.is_active:
        raise RunNotActive(run_id)
    await run.cancel(failure=reason)
    return CancelRunResponse(run=run.id, result="cancelled")


@router.post(
    "/{run_id}/retry",
    operation_id="retry_run",
    tags=["agent"],
    openapi_extra={"x-destructive": False},
    response_model=RetryRunResponse,
    response_model_by_alias=True,
)
async def retry_run(run_id: str) -> RetryRunResponse:
    """Rerun a failed run from the step that killed it, reusing every
    completed step."""
    run = Run.get(run_id)
    if not run:
        raise RunNotFound(run_id)
    if run.state != RunState.FAILED.value:
        raise RunNotFailed(run_id)

    subject = run.subject
    if subject:
        latest = Run.get_latest_for_subject(subject["type"], subject["id"])
        if latest and latest.is_active:
            raise SubjectBusy(latest.id)

    return RetryRunResponse(run=await run.retry())
