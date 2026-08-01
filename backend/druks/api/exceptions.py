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


class RunNotActive(AgentApiError):
    status_code = 409
    code = "RUN_NOT_ACTIVE"

    def __init__(self, run_id: str) -> None:
        super().__init__(f"Run {run_id} already ended.")


class RunNotFailed(AgentApiError):
    status_code = 409
    code = "RUN_NOT_FAILED"

    def __init__(self, run_id: str) -> None:
        super().__init__(f"Run {run_id} did not fail.")


class SubjectBusy(AgentApiError):
    status_code = 409
    code = "SUBJECT_BUSY"
    retryable = True

    def __init__(self, run_id: str) -> None:
        super().__init__(f"The subject already has active run {run_id}.")
