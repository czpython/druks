from enum import StrEnum


class RunState(StrEnum):
    SCHEDULED = "scheduled"
    RUNNING = "running"
    PARKED = "parked"
    FINISHED = "finished"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # The run's DBOS workflow row is gone (system tables wiped, or its executor
    # destroyed), so it will never start — terminal, not scheduled forever.
    ORPHANED = "orphaned"


ACTIVE_STATES = (RunState.SCHEDULED, RunState.RUNNING, RunState.PARKED)
TERMINAL_STATES = (RunState.FINISHED, RunState.FAILED, RunState.CANCELLED, RunState.ORPHANED)
# A subject whose newest run is in one of these is still open: going, or
# failed and wanting the operator.
OPEN_STATES = (*ACTIVE_STATES, RunState.FAILED)


class WorkflowEvent(StrEnum):
    # Each state's signal topic, and what the feed stores. Naming them makes a
    # subscriber's topic typo-proof — a bare string can silently subscribe to a
    # topic nobody publishes.
    SCHEDULED = "workflow.scheduled"
    RUNNING = "workflow.running"
    PARKED = "workflow.parked"
    FINISHED = "workflow.finished"
    FAILED = "workflow.failed"
    CANCELLED = "workflow.cancelled"
    RETRIED = "workflow.retried"

    @classmethod
    def for_state(cls, state: RunState) -> "WorkflowEvent":
        # A derived state (orphaned) announces nothing, so asking raises.
        return cls(f"workflow.{state.value}")


class AgentCallStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABANDONED = "abandoned"
