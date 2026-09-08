from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import druks.workflows as sdk
import pytest
from drukbox_sdk import Secret
from druks.sandbox.constants import SANDBOX_HOST_ROTATE_BEFORE_SECONDS
from druks.workflows import Workflow


def _profile(secrets: dict[str, Secret], secrets_id: str = "") -> SimpleNamespace:
    return SimpleNamespace(secrets=secrets, sandbox_secrets=[], secrets_id=secrets_id)


_ENTRY = Secret(
    "sk-real",
    host="api.anthropic.com",
    auth_variable="ANTHROPIC_API_KEY",
    auth_header="x-api-key",
    auth_prefix="",
)
_NONE = _profile({})
_ANTHROPIC = _profile({"anthropic": _ENTRY}, "anthropic.20260907T110000")


@dataclass
class _FakeSandbox:
    id: str
    expires_at: datetime


class _FakeSandboxClient:
    def __init__(self, *, lease: timedelta) -> None:
        self.lease = lease
        self.provisions: list[str] = []
        self.secrets: list[dict[str, Secret]] = []
        self.released: list[str] = []
        self.reattached: list[str] = []

    async def provision(
        self,
        *,
        idempotency_key: str,
        secrets: dict[str, Secret],
        template: str | None,
        identity: object = None,
    ) -> _FakeSandbox:
        assert template is None
        assert identity is None
        self.provisions.append(idempotency_key)
        self.secrets.append(secrets)
        host_id = f"host-{len(self.provisions)}"
        return _FakeSandbox(id=host_id, expires_at=datetime.now(UTC) + self.lease)

    async def release(self, *, host_id: str) -> None:
        self.released.append(host_id)

    async def reattach(self, *, host_id: str) -> _FakeSandbox:
        self.reattached.append(host_id)
        return _FakeSandbox(id=host_id, expires_at=datetime.now(UTC) + self.lease)


def _warm_workflow(*, reuse: bool = True) -> Workflow:
    # __new__ skips __init__/__init_subclass__ so the host logic can be exercised
    # without standing up DBOS; we set only what _lease_host reads.
    flow = Workflow.__new__(Workflow)
    flow.steps_reuse_sandbox = reuse
    flow._host = None
    flow._subject = None
    flow._workflow_id = "wf-1"
    return flow


@pytest.mark.asyncio
async def test_warm_host_reused_while_lease_covers_another_call(monkeypatch):
    """A warm host with lease to spare is reused across calls, never re-provisioned."""
    fake = _FakeSandboxClient(lease=timedelta(hours=2))
    monkeypatch.setattr(sdk, "sandbox_client", fake)
    flow = _warm_workflow()

    first = await flow._lease_host(_NONE)
    second = await flow._lease_host(_NONE)

    assert first == second == "host-1"
    assert fake.provisions == ["wf-1:workflow"]
    assert fake.released == []


@pytest.mark.asyncio
async def test_warm_host_rotates_when_lease_cannot_cover_a_call(monkeypatch):
    """A host whose remaining lease can't cover another worst-case call rotates
    to a fresh one before the call."""
    fake = _FakeSandboxClient(lease=timedelta(seconds=SANDBOX_HOST_ROTATE_BEFORE_SECONDS - 60))
    monkeypatch.setattr(sdk, "sandbox_client", fake)
    flow = _warm_workflow()

    first = await flow._lease_host(_NONE)
    second = await flow._lease_host(_NONE)

    assert first == "host-1"
    assert second == "host-2"
    assert fake.released == ["host-1"]
    assert fake.provisions == ["wf-1:workflow", "wf-1:workflow"]


@pytest.mark.asyncio
async def test_warm_host_keeps_its_entries_across_calls(monkeypatch):
    """Calls with the same entries keep the host created with those entries."""
    fake = _FakeSandboxClient(lease=timedelta(hours=2))
    monkeypatch.setattr(sdk, "sandbox_client", fake)
    flow = _warm_workflow()

    first = await flow._lease_host(_ANTHROPIC)
    second = await flow._lease_host(_profile({"anthropic": _ENTRY}, _ANTHROPIC.secrets_id))

    assert first == second == "host-1"
    assert fake.provisions == ["wf-1:workflow:anthropic.20260907T110000"]
    assert fake.secrets == [{"anthropic": _ENTRY}]
    assert fake.released == []


@pytest.mark.asyncio
async def test_warm_host_rotates_when_a_call_needs_other_entries(monkeypatch):
    """A host holds only the entries it was created with. A call that needs other
    entries gets a fresh host under a new provisioning key."""
    fake = _FakeSandboxClient(lease=timedelta(hours=2))
    monkeypatch.setattr(sdk, "sandbox_client", fake)
    flow = _warm_workflow()

    first = await flow._lease_host(_ANTHROPIC)
    second = await flow._lease_host(_NONE)

    assert first == "host-1"
    assert second == "host-2"
    assert fake.released == ["host-1"]
    assert fake.provisions == ["wf-1:workflow:anthropic.20260907T110000", "wf-1:workflow"]
    assert fake.secrets == [{"anthropic": _ENTRY}, {}]


@pytest.mark.asyncio
async def test_provisioning_key_names_the_pasted_key(monkeypatch):
    """A replay after a crash starts with no held host and presents its key again.
    The same pasted key finds the host. A replaced key asks for a fresh host."""
    fake = _FakeSandboxClient(lease=timedelta(hours=2))
    monkeypatch.setattr(sdk, "sandbox_client", fake)
    replaced = _profile({"anthropic": _ENTRY}, "anthropic.20260907T120000")

    await _warm_workflow()._lease_host(_ANTHROPIC)
    await _warm_workflow()._lease_host(_ANTHROPIC)
    await _warm_workflow()._lease_host(replaced)

    assert fake.provisions == [
        "wf-1:workflow:anthropic.20260907T110000",
        "wf-1:workflow:anthropic.20260907T110000",
        "wf-1:workflow:anthropic.20260907T120000",
    ]


@pytest.mark.asyncio
async def test_no_warm_host_when_reuse_disabled(monkeypatch):
    """Without steps_reuse_sandbox, each call gets its own throwaway VM, so the
    workflow never provisions or holds one."""
    fake = _FakeSandboxClient(lease=timedelta(hours=2))
    monkeypatch.setattr(sdk, "sandbox_client", fake)
    flow = _warm_workflow(reuse=False)

    assert await flow._lease_host(_NONE) is None
    assert fake.provisions == []


async def test_a_replay_finds_the_warm_box_through_its_identity(
    monkeypatch: pytest.MonkeyPatch, druks_db
) -> None:
    from conftest import connect_provider
    from druks.database import db_session
    from druks.harnesses.providers import AnthropicProvider
    from druks.sandbox.models import SandboxIdentity, SandboxSecret
    from druks.testing import seed_run
    from druks_field_notes.workflows import Summarize

    await seed_run(db_session(), kind=Summarize.kind, run_id="wf-1")
    subscription = await connect_provider(
        AnthropicProvider, {"claudeAiOauth": {"accessToken": "test-token"}}
    )
    secrets = [SandboxSecret(name="anthropic", subscription_id=subscription.id)]
    identity, _ = await SandboxIdentity.create(run_id="wf-1", scoped_to="workflow", secrets=secrets)
    await identity.bind("host-crashed")
    client = _FakeSandboxClient(lease=timedelta(hours=2))
    monkeypatch.setattr(sdk, "sandbox_client", client)
    flow = _warm_workflow()
    profile = SimpleNamespace(secrets={}, sandbox_secrets=secrets, secrets_id=subscription.id)

    assert await flow._lease_host(profile) == "host-crashed"

    assert client.reattached == ["host-crashed"]
    assert client.provisions == []
