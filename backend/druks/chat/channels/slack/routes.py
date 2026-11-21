import json
from urllib.parse import quote

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse

from druks.accounts.dependencies import current_admin_account, current_session_account
from druks.accounts.models import Account
from druks.api.dependencies import SessionDep
from druks.chat.models import Automation
from druks.chat.schemas import AutomationResponse
from druks.core.services import Slack
from druks.redis import get_client
from druks.services.exceptions import OauthPageError

from .channel import SlackChannel
from .constants import LINK_KEY

router = APIRouter(prefix="/services/slack")


@router.get("/link/{token}")
async def link_account(
    session: SessionDep,
    token: str,
    has_connected: bool = False,
    account: Account = Depends(current_session_account),
) -> RedirectResponse:
    """Answer the message that the link holds, once the signed-in person connects the
    Slack account that wrote it. Anyone else who opens the link connects only their own
    Slack account."""
    held_message = await get_client().get(LINK_KEY.format(token=token))
    if not held_message:
        raise OauthPageError("This link expired. Write to the bot again.", status_code=410)
    message = json.loads(held_message)
    card = await Slack.get()
    linked_account = await SlackChannel.lookup_account(session, card, message)
    if not linked_account and not has_connected:
        link_page = quote(f"/api/chat/services/slack/link/{token}?has_connected=true", safe="")
        return RedirectResponse(f"/api/oauth/slack/connect?next={link_page}")
    if linked_account and linked_account.id == account.id:
        conversation = await SlackChannel.save_message(
            session, card, account, message, agent_id=None
        )
        await get_client().delete(LINK_KEY.format(token=token))
        return RedirectResponse(f"/chat/{conversation.id}")
    return RedirectResponse("/chat")


@router.get(
    "/automations",
    response_model=list[AutomationResponse],
    response_model_by_alias=True,
    dependencies=[Depends(current_admin_account)],
)
async def list_automations(session: SessionDep) -> list[Automation]:
    """Every automation that tagged the bot: the ones an admin accepted, and the ones
    that wait."""
    return await Automation.list_for_connection(session, (await Slack.get()).id)


@router.post(
    "/automations/{automation_id}/accept",
    response_model=AutomationResponse,
    response_model_by_alias=True,
    dependencies=[Depends(current_admin_account)],
)
async def accept_automation(session: SessionDep, automation_id: str) -> Automation:
    """Let the automation's tags reach the default account's agent, and answer the tag
    that waited."""
    card = await Slack.get()
    automation = await Automation.get(session, id=automation_id, connection_id=card.id)
    if not automation.is_accepted:
        automation.is_accepted = True
        account = await Account.get_default(session)
        await SlackChannel.save_message(
            session, card, account, automation.first_message, agent_id=None
        )
    return automation


@router.delete(
    "/automations/{automation_id}",
    status_code=204,
    dependencies=[Depends(current_admin_account)],
)
async def remove_automation(session: SessionDep, automation_id: str) -> None:
    """Forget the automation. Its next tag waits for an admin again."""
    card = await Slack.get()
    automation = await Automation.get(session, id=automation_id, connection_id=card.id)
    await automation.delete()
