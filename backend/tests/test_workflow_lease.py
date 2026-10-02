import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import connect_provider
from drukbox_sdk.exceptions import SandboxUnavailableError
from druks.harnesses.exceptions import HarnessSandboxProvisioningError
from druks.harnesses.providers import AnthropicProvider
from druks.sandbox import client
from druks.sandbox.constants import WORKFLOW_HOST_LEASE_SECONDS
from druks.sandbox.models import SandboxIdentity
from druks.testing import seed_run


async def test_active_lease_renews_the_host_and_identity_then_stops(druks_db, monkeypatch):
    account = (await connect_provider(AnthropicProvider, {})).account_id
    run = await seed_run(druks_db, kind="test", account_id=account)
    identity, _ = await SandboxIdentity.create(
        druks_db, account_id=account, run_id=run.id, scoped_to="workflow", secret_refs=[]
    )
    await identity.bind("working-host")
    renewed = asyncio.Event()
    expiries = []

    async def renew(host_id, *, expires_at):
        assert host_id == "working-host"
        expiries.append(expires_at)
        if len(expiries) == 3:
            renewed.set()

    api = SimpleNamespace(renew_host=renew, aclose=AsyncMock())
    monkeypatch.setattr(client.Client, "_api", lambda self: api)
    monkeypatch.setattr(client, "WORKFLOW_HOST_RENEW_SECONDS", 0.01)

    async with client.sandbox_client.lease(host_id="working-host"):
        assert len(expiries) == 1
        remaining = (expiries[0] - datetime.now(UTC)).total_seconds()
        assert 590 < remaining <= WORKFLOW_HOST_LEASE_SECONDS
        await asyncio.wait_for(renewed.wait(), 2)

    await druks_db.refresh(identity)
    assert identity.expires_at == expiries[-1]
    assert expiries[-1] > expiries[0]
    count = len(expiries)
    await asyncio.sleep(0.03)
    assert len(expiries) == count


@pytest.mark.parametrize("fail_at_start", [False, True])
async def test_failed_renewal_stops_the_call(druks_db, monkeypatch, fail_at_start):
    failure = SandboxUnavailableError("control plane unavailable")
    api = SimpleNamespace(
        renew_host=AsyncMock(side_effect=[failure] if fail_at_start else [None, failure]),
        aclose=AsyncMock(),
    )
    monkeypatch.setattr(client.Client, "_api", lambda self: api)
    monkeypatch.setattr(client, "WORKFLOW_HOST_RENEW_SECONDS", 0.01)
    entered = False
    stopped = False

    with pytest.raises(HarnessSandboxProvisioningError, match="lease renewal failed"):
        async with client.sandbox_client.lease(host_id="working-host"):
            entered = True
            try:
                await asyncio.sleep(5)
                pytest.fail("A failed renewal left the call running.")
            finally:
                stopped = True

    assert entered == stopped == (not fail_at_start)


async def test_a_call_failure_keeps_its_typed_error(druks_db, monkeypatch):
    monkeypatch.setattr(client.Client, "set_expiry", AsyncMock())
    failure = ValueError("invalid prompt")
    with pytest.raises(ValueError) as raised:
        async with client.sandbox_client.lease(host_id="working-host"):
            raise failure
    assert raised.value is failure
