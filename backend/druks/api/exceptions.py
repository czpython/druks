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


def agent_error_responses(*errors: AgentApiError) -> dict:
    # One response per status; the first error listed for a status is the example.
    by_status: dict[int, list[AgentApiError]] = {}
    for error in errors:
        by_status.setdefault(error.status_code, []).append(error)
    return {
        status: {
            "description": ", ".join(error.code for error in grouped) + ".",
            "content": {
                "application/json": {
                    "example": {
                        "code": grouped[0].code,
                        "message": str(grouped[0]),
                        "retryable": grouped[0].retryable,
                    }
                }
            },
        }
        for status, grouped in by_status.items()
    }


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
