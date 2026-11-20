from fastapi import APIRouter, Depends, HTTPException, status

from druks.api.dependencies import SessionDep
from druks.api.schemas import ArtifactContent
from druks.durable.exceptions import ArtifactContentMissing
from druks.durable.models import Artifact
from druks.teams.dependencies import require_artifact_access

router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])


@router.get(
    "/{artifact_id}",
    dependencies=[Depends(require_artifact_access)],
    response_model=ArtifactContent,
)
async def get_artifact(session: SessionDep, artifact_id: str) -> ArtifactContent:
    # A call's renderable output, reached through the call that produced it — the in-app
    # review fetches this to render the plan beside its controls.
    artifact = await session.get(Artifact, artifact_id)
    if not artifact:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact not found")
    try:
        content = await artifact.read_content()
    except ArtifactContentMissing as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact content missing") from error
    return ArtifactContent(kind=artifact.kind, title=artifact.title, content=content)
