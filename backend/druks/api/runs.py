from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Path, status

from druks.accounts.context import current_account_id
from druks.api.dependencies import SessionDep
from druks.api.exceptions import (
    RunNotActive,
    RunNotFailed,
    RunNotFound,
    RunNotLatest,
    SubjectBusy,
    agent_error_responses,
)
from druks.api.schemas import CancelRunResponse, ResumeRequest, RetryRunResponse
from druks.apps.registry import workflows
from druks.durable.enums import RunState, WorkflowEvent
from druks.durable.models import Run
from druks.events.models import Event
from druks.notifications.exceptions import AnswerNotAllowedError, InvalidChoiceError
from druks.notifications.services import validate_in_app_answer

router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.post("/{run}/resume", status_code=status.HTTP_204_NO_CONTENT)
async def resume_run(
    session: SessionDep, run_id: Annotated[str, Path(alias="run")], body: ResumeRequest
) -> None:
    # The in-app half of a gate: the operator answers the parked run from Druks
    # (external gates resume through their own webhook).
    run = await session.get(Run, run_id)
    if not run:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    if run.state != RunState.PARKED.value or not run.input_request:
        raise HTTPException(status.HTTP_409_CONFLICT, "run is not waiting on an in-app decision")
    try:
        resume_payload = await validate_in_app_answer(
            session,
            run,
            account_id=current_account_id.get(),
            control=body.control,
            answers=body.answers,
            note=body.note,
        )
    except AnswerNotAllowedError as error:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(error)) from error
    except InvalidChoiceError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    await run.resume(**resume_payload)


# The "agent" tag also derives each route below into an MCP tool: its docstring
# is the tool description and operation_id is the tool name — renaming one is a
# break, never a refactor side effect.


@router.post(
    "/{run}/cancel",
    operation_id="cancel_run",
    tags=["agent"],
    openapi_extra={"x-idempotent": True},
    response_model=CancelRunResponse,
    response_model_by_alias=True,
    responses=agent_error_responses(RunNotFound("run-123"), RunNotActive("run-123")),
)
async def cancel_run(
    session: SessionDep,
    run_id: Annotated[
        str, Path(alias="run", description="The active run, from list_open_subjects.")
    ],
    reason: Annotated[
        str,
        Body(
            embed=True,
            min_length=1,
            max_length=500,
            description="The failure reason to record on the cancelled run.",
        ),
    ],
) -> CancelRunResponse:
    """Cancel an active run, recording the reason as its failure; a repeat
    cancel reports already_cancelled."""
    run = await session.get(Run, run_id)
    if not run:
        raise RunNotFound(run_id)
    if run.state == RunState.CANCELLED.value:
        return CancelRunResponse(run=run.id, result="already_cancelled")
    if not run.is_active:
        raise RunNotActive(run_id)
    subject = await run.get_subject()
    # cancel() flushes the run and expires this computed column, so read it first.
    key, title = run.subject_key, run.subject_title
    await run.cancel(failure=reason)
    if subject:
        await Event.emit(
            session,
            type=WorkflowEvent.CANCELLED,
            subject=subject,
            key=key,
            title=title,
            run=run.id,
            kind=run.kind,
            facts={"failure": reason},
            app=workflows.get(run.kind).app,
        )
    return CancelRunResponse(run=run.id, result="cancelled")


@router.post(
    "/{run}/retry",
    operation_id="retry_run",
    tags=["agent"],
    openapi_extra={"x-destructive": False},
    response_model=RetryRunResponse,
    response_model_by_alias=True,
    responses=agent_error_responses(
        RunNotFound("run-123"),
        RunNotFailed("run-123"),
        SubjectBusy("run-456"),
        RunNotLatest("run-123", "run-456"),
    ),
)
async def retry_run(
    session: SessionDep,
    run_id: Annotated[
        str, Path(alias="run", description="The failed run, from list_open_subjects.")
    ],
) -> RetryRunResponse:
    """Rerun a failed run from the step that killed it, reusing every
    completed step."""
    run = await session.get(Run, run_id)
    if not run:
        raise RunNotFound(run_id)
    if run.state != RunState.FAILED.value:
        raise RunNotFailed(run_id)

    subject = await run.get_subject()
    if subject:
        latest = await Run.get_latest_for_subject(session, subject["type"], subject["id"])
        if latest.is_active:
            raise SubjectBusy(latest.id)
        if latest.id != run.id:
            raise RunNotLatest(run_id, latest.id)

    return RetryRunResponse(run=await run.retry())
