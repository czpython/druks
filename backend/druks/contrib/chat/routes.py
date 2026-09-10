from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, status

from druks.accounts.dependencies import current_account
from druks.accounts.models import Account
from druks.contrib.chat.enums import Autonomy, Role
from druks.contrib.chat.models import Conversation
from druks.contrib.chat.workflows import Talk

router = APIRouter(prefix="/conversations")


@router.post("", status_code=status.HTTP_201_CREATED, operation_id="create_conversation")
async def create_conversation(
    body: Annotated[str, Body(embed=True)],
    account: Account = Depends(current_account),
    title: Annotated[str, Body(embed=True)] = "",
) -> dict[str, int]:
    conversation = await Conversation.create(account_id=account.id, title=title)
    await conversation.add_message(role=Role.USER, body=body)
    await Talk.dispatch(conversation=conversation)
    return {"id": conversation.id}


@router.post("/{conversation_id}/autonomy", operation_id="set_autonomy")
async def set_autonomy(
    conversation_id: int,
    autonomy: Annotated[Autonomy, Body(embed=True)],
    account: Account = Depends(current_account),
) -> dict[str, str]:
    conversation = await Conversation.get_for_account(conversation_id, account.id)
    if conversation:
        await conversation.save_autonomy(autonomy)
        return {"autonomy": conversation.autonomy}
    raise HTTPException(status.HTTP_404_NOT_FOUND, f"No conversation {conversation_id}.")
