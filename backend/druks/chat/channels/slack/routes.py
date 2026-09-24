import json
from urllib.parse import quote

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse

from druks.accounts.dependencies import current_session_account
from druks.accounts.models import Account
from druks.api.dependencies import SessionDep
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
        conversation = await SlackChannel.save_message(session, card, account, message)
        await get_client().delete(LINK_KEY.format(token=token))
        return RedirectResponse(f"/chat/{conversation.id}")
    return RedirectResponse("/chat")
