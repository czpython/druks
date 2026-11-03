from datetime import datetime

from dbos import DBOS


async def set_run_phase(phase: str, *, ends_at: datetime | None = None) -> None:
    # The live infra/agent phase, pushed as a DBOS event (transient by nature —
    # overwritten each phase, gone when the run ends). NOT a durable column.
    # A chat or browser box provisions outside a run: nothing shows its phase.
    if DBOS.workflow_id:
        await DBOS.set_event_async("phase", phase)
        if ends_at:
            await DBOS.set_event_async("phase_ends_at", ends_at)


async def get_run_phase(run_id: str) -> str | None:
    return await DBOS.get_event_async(run_id, "phase", timeout_seconds=0)


async def get_run_phase_ends_at(run_id: str) -> datetime | None:
    return await DBOS.get_event_async(run_id, "phase_ends_at", timeout_seconds=0)
