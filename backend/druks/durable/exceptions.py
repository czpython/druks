from typing import ClassVar

from druks.exceptions import AgentApiError


class FatalError(Exception):
    """End the run as failed on purpose: the message becomes the run's recorded
    failure reason and the raise reaches DBOS as the terminal outcome. Raise
    this for a deliberate domain stop, so a reader can tell it from a crash."""

    # Stamped onto the failed run beside the free-text reason, so read-sides can
    # recognize the domain stop without parsing its message. Empty for a crash.
    code: ClassVar[str] = ""


class WorkflowError(Exception):
    pass


class GateTimeout(FatalError):
    code = "gate_timeout"

    def __init__(self, gate: str) -> None:
        super().__init__(f"gate {gate!r} timed out")
        self.gate = gate


class SubjectlessGate(FatalError):
    def __init__(self, gate: str) -> None:
        super().__init__(
            f"gate {gate!r} would park a subjectless run that nobody watches — "
            "start the run with a subject, or override the gate's on_wait "
            "to notify someone directly"
        )
        self.gate = gate


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
