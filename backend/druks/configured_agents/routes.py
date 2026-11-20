from fastapi import APIRouter, Depends, Query

from druks.accounts.dependencies import current_session_account
from druks.accounts.models import Account
from druks.api.dependencies import SessionDep
from druks.configured_agents.models import ConfiguredAgent
from druks.configured_agents.schemas import AgentRequest, AgentResponse, AgentUpdateRequest
from druks.durable.models import Run
from druks.durable.reads import get_run_response
from druks.durable.schemas import RunResponse

router = APIRouter(prefix="/api/configured-agents", tags=["agents"])


@router.get("", response_model=list[AgentResponse], response_model_by_alias=True)
async def list_agents(
    session: SessionDep, account: Account = Depends(current_session_account)
) -> list[ConfiguredAgent]:
    return await ConfiguredAgent.list_for_account(session, account)


@router.post("", status_code=201, response_model=AgentResponse, response_model_by_alias=True)
async def create_agent(
    session: SessionDep, body: AgentRequest, account: Account = Depends(current_session_account)
) -> ConfiguredAgent:
    return await ConfiguredAgent.create(session, owner=account, **body.model_dump())


@router.get("/{agent_id}", response_model=AgentResponse, response_model_by_alias=True)
async def get_agent(
    agent_id: str, session: SessionDep, account: Account = Depends(current_session_account)
) -> ConfiguredAgent:
    return await ConfiguredAgent.get_for_account(session, agent_id, account)


@router.get("/{agent_id}/runs", response_model=list[RunResponse], response_model_by_alias=True)
async def list_agent_runs(
    agent_id: str,
    session: SessionDep,
    account: Account = Depends(current_session_account),
    limit: int = Query(20, ge=1, le=100),
) -> list[RunResponse]:
    agent = await ConfiguredAgent.get_for_account(session, agent_id, account)
    runs = await Run.list_for_agent(
        session, agent.id, kinds=await account.list_usable_kinds(), limit=limit
    )
    return [await get_run_response(run) for run in runs]


@router.put("/{agent_id}", response_model=AgentResponse, response_model_by_alias=True)
async def update_agent(
    agent_id: str,
    session: SessionDep,
    body: AgentUpdateRequest,
    account: Account = Depends(current_session_account),
) -> ConfiguredAgent:
    agent = await ConfiguredAgent.get_for_account(session, agent_id, account)
    await agent.replace(session, editor=account, **body.model_dump())
    return agent


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(
    agent_id: str, session: SessionDep, account: Account = Depends(current_session_account)
) -> None:
    agent = await ConfiguredAgent.get_for_account(session, agent_id, account)
    agent.check_editor(account)
    await agent.delete()
