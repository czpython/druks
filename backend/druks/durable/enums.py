from enum import StrEnum


class RunState(StrEnum):
    SCHEDULED = "scheduled"
    RUNNING = "running"
    PENDING_INPUT = "pending_input"
    FINISHED = "finished"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # The run's DBOS workflow row is gone (system tables wiped, or its executor
    # destroyed), so it will never start — terminal, not scheduled forever.
    ORPHANED = "orphaned"


ACTIVE_STATES = (RunState.SCHEDULED, RunState.RUNNING, RunState.PENDING_INPUT)
TERMINAL_STATES = (RunState.FINISHED, RunState.FAILED, RunState.CANCELLED, RunState.ORPHANED)
# A subject whose newest run is in one of these is still open: going, or
# failed and wanting the operator.
OPEN_STATES = (*ACTIVE_STATES, RunState.FAILED)


class WorkflowEvent(StrEnum):
    # What an extension subscribes to and what the feed stores. RunState above is
    # the substrate's vocabulary — DBOS statuses, the Run row — and an author
    # never types it: they know the workflow they wrote and what it did.
    RUNNING = "workflow.running"
    PARKED = "workflow.parked"
    FINISHED = "workflow.finished"
    FAILED = "workflow.failed"
    CANCELLED = "workflow.cancelled"
    STATE = "workflow.state"

    @classmethod
    def for_state(cls, state: RunState) -> "WorkflowEvent":
        # A park is our word for the substrate's pending_input; every other state
        # already names its own event. A derived state (scheduled, orphaned) never
        # announces one, so asking raises.
        if state is RunState.PENDING_INPUT:
            return cls.PARKED
        return cls(f"workflow.{state.value}")


class AgentCallStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABANDONED = "abandoned"
