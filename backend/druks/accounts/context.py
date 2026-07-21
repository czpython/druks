from contextvars import ContextVar

# The request's authenticated account, stamped by the auth gate — Workflow.start
# reads it so a browser-origin run attributes itself without route ceremony.
current_account_id: ContextVar[str | None] = ContextVar("current_account_id", default=None)
