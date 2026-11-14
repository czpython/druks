from datetime import timedelta

from druks.core import tasks
from druks.durable.constants import STALE_PARKED_FAILURE
from druks.durable.enums import RunState
from druks.durable.models import Run
from druks.models import Base
from druks.testing import seed_run
from druks.workflows import YesNo
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize
from sqlalchemy.ext.asyncio import AsyncSession


async def _parked_run(session: AsyncSession, expires_in: timedelta) -> Run:
    run = await seed_run(
        session,
        kind=Summarize.kind,
        subject=await Note.create(body="waiting for a reply"),
        state="parked",
        input_gate=YesNo.name,
    )
    run.input_expires_at = Base.utc_now() + expires_in
    await session.flush()
    return run


async def test_a_run_still_parked_an_hour_after_its_ask_expired_is_cancelled(
    druks_db, druks_without_dispatch
):
    stale_run = await _parked_run(druks_db, -timedelta(hours=2))
    just_expired_run = await _parked_run(druks_db, -timedelta(minutes=10))
    waiting_run = await _parked_run(druks_db, timedelta(days=1))

    await tasks.cancel_stale_parked_runs._function()

    for run in (stale_run, just_expired_run, waiting_run):
        await druks_db.refresh(run)
    assert stale_run.state == RunState.CANCELLED.value
    assert stale_run.failure == STALE_PARKED_FAILURE
    assert just_expired_run.state == RunState.PARKED.value
    assert waiting_run.state == RunState.PARKED.value
