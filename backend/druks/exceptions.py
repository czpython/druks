from typing import ClassVar


class AgentApiError(Exception):
    # Base for the agent surface's wire errors: each subclass names its HTTP
    # status and stable code, serialized as the one {code, message, retryable}
    # response shape. Messages are authored for the caller — never tracebacks,
    # paths, or engine internals. retryable=True marks a failure the caller can
    # fix by re-reading state and retrying.
    status_code: ClassVar[int] = 400
    code: ClassVar[str] = ""
    retryable: ClassVar[bool] = False


__all__ = ["AgentApiError"]
