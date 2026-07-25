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


class AgentCallStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABANDONED = "abandoned"
