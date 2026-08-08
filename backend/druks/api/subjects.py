from fastapi import APIRouter

from druks.api.schemas import OpenSubjectResponse, OpenSubjectsResponse, OpenWorkflowResponse
from druks.database import db_session
from druks.durable.enums import RunState
from druks.durable.models import Run

_WORKFLOW_ROWS = 50
_FAILURE_TAIL_BYTES = 512

router = APIRouter(prefix="/api", tags=["agent"])


@router.get(
    "/open-subjects",
    operation_id="list_open_subjects",
    response_model=OpenSubjectsResponse,
    response_model_by_alias=True,
)
async def list_open_subjects() -> OpenSubjectsResponse:
    """Every subject with open work — each nesting its open workflows, the
    newest run of one kind that is scheduled, running, parked, or failed.
    At most 50 workflows."""
    rows = db_session().execute(Run.get_open_subjects().limit(_WORKFLOW_ROWS)).all()
    grouped = {}
    for row in rows:
        grouped.setdefault(row.subject_type, {}).setdefault(row.subject_id, []).append(row)
    subjects = []
    for subject_type, rows_by_id in grouped.items():
        for subject_id, workflow_rows in rows_by_id.items():
            workflows = []
            for row in workflow_rows:
                failure = row.failure
                if failure:
                    failure = failure.encode()[-_FAILURE_TAIL_BYTES:].decode(errors="ignore")
                workflows.append(
                    OpenWorkflowResponse(
                        extension=row.kind.partition(".")[0],
                        state=RunState(row.state),
                        run=row.run_id,
                        latest_agent_call=row.latest_call_id,
                        failure=failure,
                        created_at=row.created_at,
                    )
                )
            newest = workflow_rows[0]
            subjects.append(
                OpenSubjectResponse(
                    subject_type=subject_type,
                    subject_id=subject_id,
                    subject_label=newest.subject_label,
                    workflows=workflows,
                )
            )
    return OpenSubjectsResponse(subjects=subjects)
