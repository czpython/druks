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

    async def provision(self, *, idempotency_key: str, template: str | None) -> _FakeSandbox:
        assert template is None
        self.provisions.append(idempotency_key)
        host_id = f"host-{len(self.provisions)}"
        return _FakeSandbox(id=host_id, expires_at=datetime.now(UTC) + self.lease)

    async def release(self, *, host_id: str) -> None:
        self.released.append(host_id)


def _warm_workflow(*, reuse: bool = True) -> Workflow:
    # __new__ skips __init__/__init_subclass__ so the host logic can be exercised
    # without standing up DBOS; we set only what _lease_host reads.
    flow = Workflow.__new__(Workflow)
    flow.steps_reuse_sandbox = reuse
    flow._host = None
    flow._workflow_id = "wf-1"
    return flow


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
