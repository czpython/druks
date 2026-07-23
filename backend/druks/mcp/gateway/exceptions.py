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


class RunNotFound(AgentApiError):
    status_code = 404
    code = "RUN_NOT_FOUND"

    def __init__(self, run_id: str) -> None:
        super().__init__(f"No run {run_id}.")


class GateNotOpen(AgentApiError):
    status_code = 409
    code = "GATE_NOT_OPEN"

    def __init__(self, run_id: str) -> None:
        super().__init__(f"Run {run_id} is not parked on a gate.")


class GateRoundStale(AgentApiError):
    status_code = 409
    code = "GATE_ROUND_STALE"
    retryable = True

    def __init__(self, run_id: str) -> None:
        super().__init__(
            f"Run {run_id} has re-parked since the parked_at you read; fetch the gate again."
        )


class GateNotAnswerable(AgentApiError):
    status_code = 409
    code = "GATE_NOT_ANSWERABLE"

    def __init__(self, run_id: str) -> None:
        super().__init__(f"The gate on run {run_id} is answered on its source, not through here.")


class InvalidGateAnswer(AgentApiError):
    code = "INVALID_GATE_ANSWER"


class AgentCallNotFound(AgentApiError):
    status_code = 404
    code = "AGENT_CALL_NOT_FOUND"

    def __init__(self, call_id: str) -> None:
        super().__init__(f"No agent call {call_id}.")


class RunNotActive(AgentApiError):
    status_code = 409
    code = "RUN_NOT_ACTIVE"

    def __init__(self, run_id: str) -> None:
        super().__init__(f"Run {run_id} already ended.")
