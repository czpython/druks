import asyncio
from contextlib import asynccontextmanager

import druks.redis
import pytest
from druks.core import tasks
from druks.harnesses.datastructures import RotationResult
from druks.sandbox import gate


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    monkeypatch.setattr(gate, "_POLL", 0.01)
    _FakeProvider.rotated_credential_ids = []


async def test_use_registers_for_the_span_and_unregisters():
    client = druks.redis.get_client()
    async with gate.use("subscription-1", "call-1"):
        assert await client.zcard("druks:sandbox:gate:users:subscription-1") == 1
    assert await client.zcard("druks:sandbox:gate:users:subscription-1") == 0


async def test_shut_grants_only_an_idle_login():
    async with gate.use("subscription-1", "call-1"):
        # subscription-1 is busy — its rotation defers; subscription-2 is idle — granted.
        async with gate.shut("subscription-1") as idle:
            assert idle is False
        async with gate.shut("subscription-2") as idle:
            assert idle is True
    # The call ended; subscription-1's next tick is granted.
    async with gate.shut("subscription-1") as idle:
        assert idle is True


async def test_new_calls_wait_out_a_shut_gate_then_proceed():
    ran: list[str] = []

    async def call() -> None:
        async with gate.use("subscription-1", "call-9"):
            ran.append("call-9")

    async with gate.shut("subscription-1"):
        pending = asyncio.create_task(call())
        await asyncio.sleep(0.05)
        assert not pending.done()  # the rotating flag blocks registration
    await asyncio.wait_for(pending, timeout=1.0)
    assert ran == ["call-9"]


async def test_expired_registrations_never_defer_a_rotation():
    # A crashed caller's registration ages out (score in the past) — shut
    # prunes it and grants instead of deferring forever.
    client = druks.redis.get_client()
    await client.zadd("druks:sandbox:gate:users:subscription-1", {"dead-call": 1.0})
    async with gate.shut("subscription-1") as idle:
        assert idle is True
    assert await client.zcard("druks:sandbox:gate:users:subscription-1") == 0


class _FakeProvider:
    id = "fake"
    due_credential_ids: set[str] = set()
    urgent_credential_ids: set[str] = set()
    rotated_credential_ids: list[str] = []

    @classmethod
    def needs_refresh(cls, subscription):
        return subscription.id in cls.due_credential_ids

    @classmethod
    def refresh_is_urgent(cls, subscription):
        return subscription.id in cls.urgent_credential_ids

    @classmethod
    async def rotate_token(cls, subscription_id):
        cls.rotated_credential_ids.append(subscription_id)
        return RotationResult(cls.id, "refreshed", subscription_id=subscription_id)


class _FakeLogin:
    provider = "fake"

    def __init__(self, subscription_id: str) -> None:
        self.id = subscription_id


class _FakeLogins:
    @classmethod
    async def list_all(cls):
        return [
            _FakeLogin(subscription_id) for subscription_id in ("subscription-1", "subscription-2")
        ]


def _fake_shut(shut: list[str], *, idle: bool):
    @asynccontextmanager
    async def fake(subscription_id: str):
        shut.append(subscription_id)
        yield idle

    return fake


async def test_refresh_shuts_only_the_due_logins(monkeypatch):
    shut: list[str] = []
    monkeypatch.setattr(tasks, "get_provider", lambda _provider_id: _FakeProvider)
    monkeypatch.setattr(tasks, "ProviderSubscription", _FakeLogins)
    monkeypatch.setattr(tasks.gate, "shut", _fake_shut(shut, idle=True))
    _FakeProvider.due_credential_ids = {"subscription-2"}
    _FakeProvider.urgent_credential_ids = set()

    result = await tasks._refresh()

    # Only the due subscription's gate shut; every row still rotated (the fresh one
    # no-ops inside rotate_token itself).
    assert shut == ["subscription-2"]
    assert [r["action"] for r in result["results"]] == ["refreshed", "refreshed"]


async def test_refresh_defers_a_busy_login(monkeypatch):
    shut: list[str] = []
    monkeypatch.setattr(tasks, "get_provider", lambda _provider_id: _FakeProvider)
    monkeypatch.setattr(tasks, "ProviderSubscription", _FakeLogins)
    monkeypatch.setattr(tasks.gate, "shut", _fake_shut(shut, idle=False))
    _FakeProvider.due_credential_ids = {"subscription-2"}
    _FakeProvider.urgent_credential_ids = set()

    result = await tasks._refresh()

    assert [r["action"] for r in result["results"]] == ["refreshed", "busy"]


async def test_refresh_rotates_a_busy_credential_once_urgent(monkeypatch):
    # Expiry inside the call horizon: a mid-run 401 is unavoidable either way,
    # so the rotation no longer defers.
    shut: list[str] = []
    monkeypatch.setattr(tasks, "get_provider", lambda _provider_id: _FakeProvider)
    monkeypatch.setattr(tasks, "ProviderSubscription", _FakeLogins)
    monkeypatch.setattr(tasks.gate, "shut", _fake_shut(shut, idle=False))
    _FakeProvider.due_credential_ids = {"subscription-2"}
    _FakeProvider.urgent_credential_ids = {"subscription-2"}

    result = await tasks._refresh()

    assert [r["action"] for r in result["results"]] == ["refreshed", "refreshed"]


async def test_refresh_touches_no_gate_on_a_no_op_tick(monkeypatch):
    shut: list[str] = []
    monkeypatch.setattr(tasks, "get_provider", lambda _provider_id: _FakeProvider)
    monkeypatch.setattr(tasks, "ProviderSubscription", _FakeLogins)
    monkeypatch.setattr(tasks.gate, "shut", _fake_shut(shut, idle=True))
    _FakeProvider.due_credential_ids = set()
    _FakeProvider.urgent_credential_ids = set()

    await tasks._refresh()

    assert shut == []
