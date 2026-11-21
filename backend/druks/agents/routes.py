from dbos import DBOS
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import AwareDatetime

from druks.accounts.dependencies import current_session_account
from druks.accounts.models import Account
from druks.agents import schemas
from druks.agents.models import Agent
from druks.api.dependencies import SessionDep
from druks.api.schemas import AgentScheduleResponse, ScheduledRun
from druks.durable.dbos_state import latest_invocations
from druks.durable.models import AgentCall, Run, Schedule
from druks.durable.reads import get_run_response
from druks.durable.schemas import RunResponse
from druks.exceptions import PermissionDenied
from druks.notifications.services import list_notification_channels

router = APIRouter(prefix="/api/agents", tags=["agents"])


async def require_agent_editor(
    request: Request, session: SessionDep, account: Account = Depends(current_session_account)
) -> None:
    """The caller must be the agent's owner or an admin. An agent the caller cannot see
    answers 404."""
    agent = await Agent.get_for_account(session, request.path_params["agent_id"], account)
    if not account.can_edit(agent):
        raise PermissionDenied("Only the agent's owner or an admin can change it.")


@router.get("", response_model=list[schemas.AgentResponse], response_model_by_alias=True)
async def list_agents(
    session: SessionDep, account: Account = Depends(current_session_account)
) -> list[Agent]:
    return await Agent.list_for_account(session, account)


@router.post(
    "", status_code=201, response_model=schemas.AgentResponse, response_model_by_alias=True
)
async def create_agent(
    session: SessionDep,
    body: schemas.AgentRequest,
    account: Account = Depends(current_session_account),
) -> Agent:
    return await Agent.create(session, owner=account, **body.model_dump())


@router.get("/{agent_id}", response_model=schemas.AgentResponse, response_model_by_alias=True)
async def get_agent(
    agent_id: str, session: SessionDep, account: Account = Depends(current_session_account)
) -> Agent:
    return await Agent.get_for_account(session, agent_id, account)


@router.get("/{agent_id}/runs", response_model=list[RunResponse], response_model_by_alias=True)
async def list_agent_runs(
    agent_id: str,
    session: SessionDep,
    account: Account = Depends(current_session_account),
    limit: int = Query(20, ge=1, le=100),
) -> list[RunResponse]:
    agent = await Agent.get_for_account(session, agent_id, account)
    runs = await Run.list_for_agent(
        session, agent.id, kinds=await account.list_usable_kinds(), limit=limit
    )
    return [await get_run_response(run) for run in runs]


@router.get(
    "/{agent_id}/schedules",
    response_model=list[AgentScheduleResponse],
    response_model_by_alias=True,
)
async def list_agent_schedules(
    agent_id: str,
    request: Request,
    session: SessionDep,
    account: Account = Depends(current_session_account),
) -> list[AgentScheduleResponse]:
    agent = await Agent.get_for_account(session, agent_id, account)
    usable_apps = {app.name for app in await account.list_usable_apps()}
    usable_tools = [
        tool for tool, app in request.app.state.tool_apps.items() if not app or app in usable_apps
    ]
    schedules = await Schedule.list_for_agent(session, agent.id, tools=usable_tools)
    history = await session.execute(
        latest_invocations([schedule.schedule_name for schedule in schedules], limit=1)
    )
    last_runs = {row.schedule_name: ScheduledRun.model_validate(row) for row in history}
    return [
        AgentScheduleResponse.from_schedule(
            schedule, last_run=last_runs.get(schedule.schedule_name)
        )
        for schedule in schedules
    ]


@router.get(
    "/{agent_id}/cost", response_model=schemas.AgentCostResponse, response_model_by_alias=True
)
async def get_agent_cost(
    agent_id: str,
    session: SessionDep,
    since: AwareDatetime,
    account: Account = Depends(current_session_account),
) -> schemas.AgentCostResponse:
    agent = await Agent.get_for_account(session, agent_id, account)
    return schemas.AgentCostResponse(
        cost_usd=await AgentCall.get_cost_for_agent(
            session, agent.id, kinds=await account.list_usable_kinds(), since=since
        )
    )


@router.put(
    "/{agent_id}",
    response_model=schemas.AgentResponse,
    response_model_by_alias=True,
    dependencies=[Depends(require_agent_editor)],
)
async def update_agent(
    agent_id: str,
    session: SessionDep,
    body: schemas.AgentUpdateRequest,
    account: Account = Depends(current_session_account),
) -> Agent:
    agent = await Agent.get_for_account(session, agent_id, account)
    channel_id = body.channel_id
    if (
        channel_id
        and channel_id != agent.channel_id
        and channel_id not in [channel["id"] for channel in await list_notification_channels()]
    ):
        raise HTTPException(
            status_code=422,
            detail=f"The bot is not in {channel_id}. Invite it to the channel first.",
        )
    await agent.replace(session, editor=account, **body.model_dump())
    return agent


@router.delete("/{agent_id}", status_code=204, dependencies=[Depends(require_agent_editor)])
async def delete_agent(
    agent_id: str, session: SessionDep, account: Account = Depends(current_session_account)
) -> None:
    agent = await Agent.get_for_account(session, agent_id, account)
    # The cascade deletes the schedule rows, so read them first; their DBOS schedules
    # would otherwise keep firing.
    for schedule in await Schedule.list_for_agent(session, agent.id):
        await DBOS.delete_schedule_async(schedule.schedule_name)
    await agent.delete()
