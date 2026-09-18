from typing import Annotated

from fastapi import Depends, HTTPException

from druks.accounts.context import current_conversation_id
from druks.accounts.dependencies import current_account
from druks.accounts.models import Account
from druks.api.dependencies import SessionDep
from druks.chat import datastructures
from druks.chat.models import Conversation


async def get_bot_user(
    session: SessionDep, account: Account = Depends(current_account)
) -> datastructures.BotUser:
    """The person who writes in the conversation that the tool call came from.
    ``current_account`` has checked that the account owns that conversation."""
    if conversation_id := current_conversation_id.get():
        conversation = await session.get(Conversation, conversation_id)
        if conversation.user_id:
            return datastructures.BotUser(
                id=conversation.user_id,
                name=conversation.user_name,
                phone=conversation.user_phone,
                source=conversation.source,
            )
    raise HTTPException(
        status_code=409,
        detail="This tool serves the person in a chat. Call it from a chat conversation.",
    )


# Resolved from the request, never from a tool input, so it never enters a tool's schema.
BotUser = Annotated[datastructures.BotUser, Depends(get_bot_user)]
