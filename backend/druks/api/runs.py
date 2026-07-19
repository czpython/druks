from fastapi import APIRouter, HTTPException, status

from druks.api.schemas import ResumeRequest
from druks.notifications.exceptions import InvalidChoiceError
from druks.notifications.services import validate_in_app_answer
from druks.workflows import Run, RunState

router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.post("/{run_id}/resume", status_code=status.HTTP_204_NO_CONTENT)
async def resume_run(run_id: str, body: ResumeRequest) -> None:
    # The in-app half of a gate: the operator answers the parked run from Druks
    # (external gates resume through their own webhook).
    run = Run.get(run_id)
    if not run:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    ask = run.input_request
    if run.state != RunState.PENDING_INPUT.value or not ask:
        raise HTTPException(status.HTTP_409_CONFLICT, "run is not waiting on an in-app decision")
    try:
        resume_payload = validate_in_app_answer(ask, body.control, body.answers, body.note)
    except InvalidChoiceError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    await run.resume(**resume_payload)
