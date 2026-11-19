from fastapi import APIRouter, Depends

from druks.accounts.dependencies import current_session_account
from druks.accounts.models import Account
from druks.api.dependencies import SessionDep
from druks.configured_agents.models import ConfiguredAgent
from druks.configured_agents.schemas import AgentRequest, AgentResponse, AgentUpdateRequest

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
