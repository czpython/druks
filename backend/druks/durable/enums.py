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
        return _EVENT_BY_STATE[state]


# Only the states a transition announces; SCHEDULED and ORPHANED are derived, so
# a lookup for either is a bug worth the KeyError.
_EVENT_BY_STATE = {
    RunState.RUNNING: WorkflowEvent.RUNNING,
    RunState.PENDING_INPUT: WorkflowEvent.PARKED,
    RunState.FINISHED: WorkflowEvent.FINISHED,
    RunState.FAILED: WorkflowEvent.FAILED,
    RunState.CANCELLED: WorkflowEvent.CANCELLED,
}


class AgentCallStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABANDONED = "abandoned"
