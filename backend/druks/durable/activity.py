from dbos import DBOS


async def set_run_phase(phase: str) -> None:
    # The live infra/agent phase, pushed as a DBOS event (transient by nature —
    # overwritten each phase, gone when the run ends). NOT a durable column.
    # A chat or browser box provisions outside a run: nothing shows its phase.
    if DBOS.workflow_id:
        await DBOS.set_event_async("phase", phase)


async def get_run_phase(run_id: str) -> str | None:
    return await DBOS.get_event_async(run_id, "phase", timeout_seconds=0)
