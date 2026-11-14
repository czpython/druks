from contextvars import ContextVar
from typing import NamedTuple


class Actor(NamedTuple):
    """The person whose request a webhook delivery is, as a provider knows them."""

    authority: str
    user_id: str


# The request's authenticated account, stamped by the auth gate — Workflow.start
# reads it so a browser-origin run attributes itself without route ceremony.
current_account_id: ContextVar[str | None] = ContextVar("current_account_id", default=None)
# The person behind the webhook delivery that Druks handles. Workflow.start reads it.
current_actor: ContextVar[Actor | None] = ContextVar("current_actor", default=None)
# The chat conversation an agent's tool call came from. The same gate checks that
# the account owns it, and Workflow.start records it as the run's conversation.
current_conversation_id: ContextVar[str | None] = ContextVar(
    "current_conversation_id", default=None
)
