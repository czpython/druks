from typing import ClassVar


class FatalError(Exception):
    """End the run as failed on purpose: the message becomes the run's recorded
    failure reason and the raise reaches DBOS as the terminal outcome. Raise
    this for a deliberate domain stop, so a reader can tell it from a crash."""

    # Stamped onto the failed run beside the free-text reason, so read-sides can
    # recognize the domain stop without parsing its message. Empty for a crash.
    code: ClassVar[str] = ""


class OperatorCancelled(Exception):
    """End the run as cancelled on the operator's say-so: the message becomes the
    run's recorded reason and the run finalizes as CANCELLED, not FAILED — a
    deliberate stop the board reads as done, not as still needing you."""


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
