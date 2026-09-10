from typing import Annotated

from fastapi import APIRouter, Body, Depends, status

from druks.accounts.dependencies import current_account
from druks.accounts.models import Account
from druks.contrib.chat.enums import Role
from druks.contrib.chat.models import Conversation
from druks.contrib.chat.workflows import Talk

router = APIRouter(prefix="/conversations")


@router.post("", status_code=status.HTTP_201_CREATED, operation_id="create_conversation")
async def create_conversation(
    body: Annotated[str, Body(embed=True)],
    account: Account = Depends(current_account),
) -> dict[str, int]:
    conversation = await Conversation.create(account_id=account.id, title="")
    await conversation.add_message(role=Role.USER, body=body)
    await Talk.dispatch(conversation=conversation)
    return {"id": conversation.id}
