from fastapi import APIRouter, HTTPException, status

from druks.api.schemas import ResumeRequest
from druks.durable import agent
from druks.durable.exceptions import (
    GateNotAnswerable,
    GateNotOpen,
    GateRoundStale,
    InvalidGateAnswer,
)
from druks.workflows import Run

router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.post("/{run_id}/resume", status_code=status.HTTP_204_NO_CONTENT)
async def resume_run(run_id: str, body: ResumeRequest) -> None:
    # The in-app half of a gate: the operator answers the parked run from Druks
    # (external gates resume through their own webhook). The dashboard always
    # answers the live park, so this echoes the run's own parked_at into the
    # shared answer service and keeps its historical wire contract (204 / 409 /
    # 422) over the agent taxonomy.
    run = Run.get(run_id)
    if not run:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    parked_at = run.input_requested_at
    if not parked_at:
        raise HTTPException(status.HTTP_409_CONFLICT, "run is not waiting on an in-app decision")
    try:
        result = await agent.answer_gate(
            run_id,
            parked_at=parked_at,
            control=body.control,
            answers=body.answers,
            note=body.note,
        )
    except (GateNotOpen, GateRoundStale) as error:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "run is not waiting on an in-app decision"
        ) from error
    except (GateNotAnswerable, InvalidGateAnswer) as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    if result.result == "already_answered":
        # This park was already resumed; the double-submit stays the conflict
        # this route has always reported.
        raise HTTPException(status.HTTP_409_CONFLICT, "run is not waiting on an in-app decision")
