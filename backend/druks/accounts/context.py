from contextvars import ContextVar

# The request's authenticated account, stamped by the auth gate — Workflow.start
# reads it so a browser-origin run attributes itself without route ceremony.
current_account_id: ContextVar[str | None] = ContextVar("current_account_id", default=None)
# The chat conversation an agent's tool call came from. The same gate checks that
# the account owns it, and Workflow.start records it as the run's conversation.
current_conversation_id: ContextVar[str | None] = ContextVar(
    "current_conversation_id", default=None
)
