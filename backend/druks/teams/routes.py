from fastapi import APIRouter, Depends

from druks.accounts.dependencies import current_admin_account
from druks.api.dependencies import SessionDep
from druks.teams.models import Team
from druks.teams.schemas import TeamRequest, TeamResponse

router = APIRouter(
    prefix="/api/teams", tags=["teams"], dependencies=[Depends(current_admin_account)]
)


@router.get("", response_model=list[TeamResponse], response_model_by_alias=True)
async def list_teams(session: SessionDep) -> list[Team]:
    return await Team.list_all(session)


@router.post("", status_code=201, response_model=TeamResponse, response_model_by_alias=True)
async def create_team(session: SessionDep, body: TeamRequest) -> Team:
    return await Team.create(session, name=body.name, member_ids=body.member_ids, apps=body.apps)


@router.put("/{team_id}", response_model=TeamResponse, response_model_by_alias=True)
async def update_team(team_id: str, session: SessionDep, body: TeamRequest) -> Team:
    team = await Team.get(session, id=team_id)
    await team.replace(session, name=body.name, member_ids=body.member_ids, apps=body.apps)
    return team


@router.delete("/{team_id}", status_code=204)
async def delete_team(team_id: str, session: SessionDep) -> None:
    team = await Team.get(session, id=team_id)
    await team.delete()
