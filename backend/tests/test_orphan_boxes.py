from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from conftest import connect_provider
from druks.core import tasks
from druks.database import db_session
from druks.durable.engine import _step_engine
from druks.harnesses.providers import AnthropicProvider
from druks.sandbox.models import SandboxIdentity, SecretRef
from druks.testing import seed_run
from druks_field_notes.workflows import Summarize


async def _identity(run_id: str, *, state: str = "running", host_id: str = "") -> SandboxIdentity:
    await seed_run(db_session(), kind=Summarize.kind, run_id=run_id, state=state)
    subscription = await connect_provider(
        AnthropicProvider, {"claudeAiOauth": {"accessToken": "test-token"}}
    )
    identity, _ = await SandboxIdentity.create(
        run_id=run_id,
        scoped_to="workflow",
        secret_refs=[SecretRef(name="anthropic", secret_id=subscription.id)],
    )
    if host_id:
        await identity.bind(host_id)
    return identity


def _drukbox(monkeypatch) -> list[str]:
    # A release ends the identity first, as the real client does.
    released: list[str] = []

    async def release(*, host_id: str):
        released.append(host_id)
        await SandboxIdentity.revoke_for_host(_step_engine(), host_id)

    monkeypatch.setattr(tasks, "sandbox_client", SimpleNamespace(release=release))
    return released


async def test_a_finished_runs_box_is_released_and_a_live_runs_box_stays(druks_db, monkeypatch):
    await _identity("run-1", state="finished", host_id="host-dead")
    await _identity("run-2", host_id="host-live")
    released = _drukbox(monkeypatch)

    await tasks._release_orphan_boxes()

    assert released == ["host-dead"]


async def test_a_box_past_its_lease_is_left_to_drukbox(druks_db, monkeypatch):
    identity = await _identity("run-1", state="finished", host_id="host-old")
    identity.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session().commit()
    released = _drukbox(monkeypatch)

    await tasks._release_orphan_boxes()

    assert released == []


async def test_an_identity_without_a_box_has_nothing_to_release(druks_db, monkeypatch):
    await _identity("run-1", state="finished")
    released = _drukbox(monkeypatch)

    await tasks._release_orphan_boxes()

    assert released == []


async def test_a_second_tick_finds_nothing_to_do(druks_db, monkeypatch):
    await _identity("run-1", state="finished", host_id="host-dead")
    released = _drukbox(monkeypatch)

    await tasks._release_orphan_boxes()
    await tasks._release_orphan_boxes()

    assert released == ["host-dead"]
