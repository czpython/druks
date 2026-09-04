from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import druks.workflows as sdk
import pytest
from druks.sandbox.constants import SANDBOX_HOST_ROTATE_BEFORE_SECONDS
from druks.workflows import Workflow


@dataclass
class _FakeSandbox:
    id: str
    expires_at: datetime


class _FakeSandboxClient:
    def __init__(self, *, lease: timedelta) -> None:
        self.lease = lease
        self.provisions: list[str] = []
        self.released: list[str] = []
        self.expiry_sets: list[tuple[str, datetime]] = []

    async def provision(self, *, idempotency_key: str, template: str | None) -> _FakeSandbox:
        assert template is None
        self.provisions.append(idempotency_key)
        host_id = f"host-{len(self.provisions)}"
        return _FakeSandbox(id=host_id, expires_at=datetime.now(UTC) + self.lease)

    async def release(self, *, host_id: str) -> None:
        self.released.append(host_id)

    async def set_expiry(self, *, host_id: str, expires_at: datetime) -> None:
        self.expiry_sets.append((host_id, expires_at))


def _warm_workflow(*, reuse: bool = True) -> Workflow:
    # __new__ skips __init__/__init_subclass__ so the host logic can be exercised
    # without standing up DBOS; we set only what _lease_host reads.
    flow = Workflow.__new__(Workflow)
    flow.steps_reuse_sandbox = reuse
    flow._host = None
    flow._subject = None
    flow._workflow_id = "wf-1"
    return flow


def _park_without_dbos(monkeypatch) -> None:
    # The park's durable surroundings — the run event it emits and the channel it
    # suspends on — say nothing about the hold, so the gate answers immediately.
    async def _emit(*args, **kwargs) -> None:
        return

    async def _answer(gate, timeout_seconds=None) -> dict[str, str]:
        return {"action": "approve"}

    monkeypatch.setattr(sdk, "_emit_run_event", _emit)
    monkeypatch.setattr(sdk.DBOS, "recv_async", _answer)


@pytest.mark.asyncio
async def test_warm_host_reused_while_lease_covers_another_call(monkeypatch):
    """A warm host with lease to spare is reused across calls, never re-provisioned."""
    fake = _FakeSandboxClient(lease=timedelta(hours=2))
    monkeypatch.setattr(sdk, "sandbox_client", fake)
    flow = _warm_workflow()

    first = await flow._lease_host()
    second = await flow._lease_host()

    assert first == second == "host-1"
    assert fake.provisions == ["wf-1:sandbox"]
    assert fake.released == []


@pytest.mark.asyncio
async def test_warm_host_rotates_when_lease_cannot_cover_a_call(monkeypatch):
    """A host whose remaining lease can't cover another worst-case call rotates
    to a fresh one before the call."""
    fake = _FakeSandboxClient(lease=timedelta(seconds=SANDBOX_HOST_ROTATE_BEFORE_SECONDS - 60))
    monkeypatch.setattr(sdk, "sandbox_client", fake)
    flow = _warm_workflow()

    first = await flow._lease_host()
    second = await flow._lease_host()

    assert first == "host-1"
    assert second == "host-2"
    assert fake.released == ["host-1"]
    assert fake.provisions == ["wf-1:sandbox", "wf-1:sandbox"]


@pytest.mark.asyncio
async def test_no_warm_host_when_reuse_disabled(monkeypatch):
    """Without steps_reuse_sandbox, each call gets its own throwaway VM, so the
    workflow never provisions or holds one."""
    fake = _FakeSandboxClient(lease=timedelta(hours=2))
    monkeypatch.setattr(sdk, "sandbox_client", fake)
    flow = _warm_workflow(reuse=False)

    assert await flow._lease_host() is None
    assert fake.provisions == []


@pytest.mark.asyncio
async def test_park_without_hold_releases_the_warm_host(monkeypatch):
    """A park with no hold is today's park: the VM goes, nothing is clipped."""
    fake = _FakeSandboxClient(lease=timedelta(hours=2))
    monkeypatch.setattr(sdk, "sandbox_client", fake)
    _park_without_dbos(monkeypatch)
    flow = _warm_workflow()
    await flow._lease_host()

    await sdk._park(flow, "review", None, 60.0)

    assert fake.released == ["host-1"]
    assert fake.expiry_sets == []
    assert flow._host is None


@pytest.mark.asyncio
async def test_park_with_hold_clips_the_lease_and_keeps_the_host(monkeypatch):
    """A held park clips the lease instead of deleting the VM, and the run keeps
    the handle so a same-worker resume reattaches warm."""
    fake = _FakeSandboxClient(lease=timedelta(hours=2))
    monkeypatch.setattr(sdk, "sandbox_client", fake)
    _park_without_dbos(monkeypatch)
    flow = _warm_workflow()
    await flow._lease_host()

    await sdk._park(flow, "review", None, 60.0, hold_sandbox=True)

    assert [host_id for host_id, _ in fake.expiry_sets] == ["host-1"]
    assert fake.released == []
    assert flow._host is not None
    assert flow._host.id == "host-1"


@pytest.mark.asyncio
async def test_hold_true_clips_to_one_more_worst_case_call(monkeypatch):
    """``True`` holds the VM for as long as its lease could still cover one more
    worst-case call — past that the next call would rotate anyway."""
    fake = _FakeSandboxClient(lease=timedelta(hours=2))
    monkeypatch.setattr(sdk, "sandbox_client", fake)
    flow = _warm_workflow()
    await flow._lease_host()

    await flow._hold_host(True)

    ((_, expires_at),) = fake.expiry_sets
    clip = datetime.now(UTC) + timedelta(seconds=SANDBOX_HOST_ROTATE_BEFORE_SECONDS)
    assert clip - timedelta(seconds=5) <= expires_at <= clip
    assert expires_at <= flow._host.expires_at


@pytest.mark.asyncio
async def test_hold_never_outlasts_the_lease_drukbox_granted(monkeypatch):
    """The clip is a floor, never an extension: a lease shorter than the hold
    stands as it is."""
    fake = _FakeSandboxClient(lease=timedelta(minutes=20))
    monkeypatch.setattr(sdk, "sandbox_client", fake)
    flow = _warm_workflow()
    await flow._lease_host()

    await flow._hold_host(timedelta(hours=1))

    assert fake.expiry_sets == [("host-1", flow._host.expires_at)]


@pytest.mark.asyncio
async def test_hold_timedelta_clips_to_the_requested_span(monkeypatch):
    """A timedelta hold ends at ``now + hold`` when the lease outlasts it."""
    fake = _FakeSandboxClient(lease=timedelta(hours=2))
    monkeypatch.setattr(sdk, "sandbox_client", fake)
    flow = _warm_workflow()
    await flow._lease_host()

    await flow._hold_host(timedelta(minutes=30))

    ((_, expires_at),) = fake.expiry_sets
    clip = datetime.now(UTC) + timedelta(minutes=30)
    assert clip - timedelta(seconds=5) <= expires_at <= clip


@pytest.mark.asyncio
async def test_hold_without_a_warm_host_touches_nothing(monkeypatch):
    """Without steps_reuse_sandbox there is no warm host to hold, so a held park
    neither clips nor deletes."""
    fake = _FakeSandboxClient(lease=timedelta(hours=2))
    monkeypatch.setattr(sdk, "sandbox_client", fake)
    _park_without_dbos(monkeypatch)
    flow = _warm_workflow(reuse=False)
    await flow._lease_host()

    await sdk._park(flow, "review", None, 60.0, hold_sandbox=True)

    assert fake.expiry_sets == []
    assert fake.released == []
    assert fake.provisions == []


@pytest.mark.asyncio
async def test_resume_after_a_hold_reuses_the_held_host(monkeypatch):
    """The worker that survived the recv still holds the handle, so the first
    call after the resume lands on the same VM."""
    fake = _FakeSandboxClient(lease=timedelta(hours=2))
    monkeypatch.setattr(sdk, "sandbox_client", fake)
    _park_without_dbos(monkeypatch)
    flow = _warm_workflow()
    await flow._lease_host()
    await sdk._park(flow, "review", None, 60.0, hold_sandbox=True)

    assert await flow._lease_host() == "host-1"
    assert fake.provisions == ["wf-1:sandbox"]


@pytest.mark.asyncio
async def test_resume_on_a_restarted_worker_re_leases_under_the_run_key(monkeypatch):
    """A worker that died over the park has no handle: the resume goes back
    through the run's idempotency key exactly once — warm if the clipped lease
    still stands, cold if drukbox already reaped it."""
    fake = _FakeSandboxClient(lease=timedelta(hours=2))
    monkeypatch.setattr(sdk, "sandbox_client", fake)
    _park_without_dbos(monkeypatch)
    flow = _warm_workflow()
    await flow._lease_host()
    await sdk._park(flow, "review", None, 60.0, hold_sandbox=timedelta(minutes=30))
    flow._host = None

    await flow._lease_host()

    assert fake.provisions == ["wf-1:sandbox", "wf-1:sandbox"]
    assert fake.released == []
