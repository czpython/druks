from fastapi import APIRouter, HTTPException, status

from druks.api.schemas import ArtifactContent
from druks.database import db_session
from druks.durable.models import AgentCall, Artifact

router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])


@router.get("/{artifact_id}", response_model=ArtifactContent)
async def get_artifact(artifact_id: str) -> ArtifactContent:
    # A call's renderable output, reached through the call that produced it — the in-app
    # review fetches this to render the plan beside its controls.
    artifact = db_session().get(Artifact, artifact_id)
    if not artifact:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact not found")
    call = AgentCall.get(artifact.agent_call_id)
    path = call.get_file_path(artifact.path)
    if not path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact content missing")
    return ArtifactContent(kind=artifact.kind, title=artifact.title, content=path.read_text())
