import asyncio
from contextlib import asynccontextmanager

import druks.redis
import pytest
from druks.core import tasks as harness_workflows
from druks.harnesses.datastructures import RotationResult
from druks.sandbox import gate


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    monkeypatch.setattr(gate, "_POLL", 0.01)
    _FakeHarness.rotated_connection_ids = []
    _FakeConnections.refresh_capable_connection_ids = {"login-1", "login-2"}


async def test_use_registers_for_the_span_and_unregisters():
    client = druks.redis.get_client()
    async with gate.use("login-1", "call-1"):
        assert await client.zcard("druks:sandbox:gate:users:login-1") == 1
    assert await client.zcard("druks:sandbox:gate:users:login-1") == 0


async def test_shut_grants_only_an_idle_login():
    async with gate.use("login-1", "call-1"):
        # login-1 is busy — its rotation defers; login-2 is idle — granted.
        async with gate.shut("login-1") as idle:
            assert idle is False
        async with gate.shut("login-2") as idle:
            assert idle is True
    # The call ended; login-1's next tick is granted.
    async with gate.shut("login-1") as idle:
        assert idle is True


async def test_new_calls_wait_out_a_shut_gate_then_proceed():
    ran: list[str] = []

    async def call() -> None:
        async with gate.use("login-1", "call-9"):
            ran.append("call-9")

    async with gate.shut("login-1"):
        pending = asyncio.create_task(call())
        await asyncio.sleep(0.05)
        assert not pending.done()  # the rotating flag blocks registration
    await asyncio.wait_for(pending, timeout=1.0)
    assert ran == ["call-9"]


async def test_expired_registrations_never_defer_a_rotation():
    # A crashed caller's registration ages out (score in the past) — shut
    # prunes it and grants instead of deferring forever.
    client = druks.redis.get_client()
    await client.zadd("druks:sandbox:gate:users:login-1", {"dead-call": 1.0})
    async with gate.shut("login-1") as idle:
        assert idle is True
    assert await client.zcard("druks:sandbox:gate:users:login-1") == 0


class _FakeHarness:
    name = "fake"
    due_connection_ids: set[str] = set()
    urgent_connection_ids: set[str] = set()
    rotated_connection_ids: list[str] = []

    @classmethod
    def needs_refresh(cls, login):
        return login.id in cls.due_connection_ids

    @classmethod
    def refresh_is_urgent(cls, login):
        return login.id in cls.urgent_connection_ids

    @classmethod
    async def rotate_token(cls, connection_id):
        cls.rotated_connection_ids.append(connection_id)
        return RotationResult(cls.name, "refreshed", connection_id=connection_id)


class _FakeConnection:
    harness = "fake"

    def __init__(self, connection_id: str, *, supports_refresh: bool) -> None:
        self.id = connection_id
        self.supports_refresh = supports_refresh


class _FakeConnections:
    refresh_capable_connection_ids = {"login-1", "login-2"}

    @classmethod
    async def list_all(cls):
        return [
            _FakeConnection(
                connection_id,
                supports_refresh=connection_id in cls.refresh_capable_connection_ids,
            )
            for connection_id in ("login-1", "login-2")
        ]


def _fake_shut(shut: list[str], *, idle: bool):
    @asynccontextmanager
    async def fake(connection_id: str):
        shut.append(connection_id)
        yield idle

    return fake


async def test_refresh_shuts_only_the_due_logins(monkeypatch):
    shut: list[str] = []
    monkeypatch.setattr(harness_workflows, "get_harnesses", lambda: (_FakeHarness,))
    monkeypatch.setattr(harness_workflows, "HarnessConnection", _FakeConnections)
    monkeypatch.setattr(harness_workflows.gate, "shut", _fake_shut(shut, idle=True))
    _FakeHarness.due_connection_ids = {"login-2"}
    _FakeHarness.urgent_connection_ids = set()

    result = await harness_workflows._refresh()

    # Only the due login's gate shut; every row still rotated (the fresh one
    # no-ops inside rotate_token itself).
    assert shut == ["login-2"]
    assert [r["action"] for r in result["results"]] == ["refreshed", "refreshed"]


async def test_refresh_skips_a_connection_that_does_not_refresh(monkeypatch):
    shut: list[str] = []
    monkeypatch.setattr(harness_workflows, "get_harnesses", lambda: (_FakeHarness,))
    monkeypatch.setattr(harness_workflows, "HarnessConnection", _FakeConnections)
    monkeypatch.setattr(harness_workflows.gate, "shut", _fake_shut(shut, idle=True))
    _FakeHarness.due_connection_ids = {"login-1", "login-2"}
    _FakeHarness.urgent_connection_ids = set()
    _FakeConnections.refresh_capable_connection_ids = {"login-1"}

    result = await harness_workflows._refresh()

    assert shut == ["login-1"]
    assert _FakeHarness.rotated_connection_ids == ["login-1"]
    assert [row["connection_id"] for row in result["results"]] == ["login-1"]


async def test_refresh_defers_a_busy_login(monkeypatch):
    shut: list[str] = []
    monkeypatch.setattr(harness_workflows, "get_harnesses", lambda: (_FakeHarness,))
    monkeypatch.setattr(harness_workflows, "HarnessConnection", _FakeConnections)
    monkeypatch.setattr(harness_workflows.gate, "shut", _fake_shut(shut, idle=False))
    _FakeHarness.due_connection_ids = {"login-2"}
    _FakeHarness.urgent_connection_ids = set()

    result = await harness_workflows._refresh()

    assert [r["action"] for r in result["results"]] == ["refreshed", "busy"]


async def test_refresh_rotates_a_busy_login_once_urgent(monkeypatch):
    # Expiry inside the call horizon: a mid-run 401 is unavoidable either way,
    # so the rotation no longer defers.
    shut: list[str] = []
    monkeypatch.setattr(harness_workflows, "get_harnesses", lambda: (_FakeHarness,))
    monkeypatch.setattr(harness_workflows, "HarnessConnection", _FakeConnections)
    monkeypatch.setattr(harness_workflows.gate, "shut", _fake_shut(shut, idle=False))
    _FakeHarness.due_connection_ids = {"login-2"}
    _FakeHarness.urgent_connection_ids = {"login-2"}

    result = await harness_workflows._refresh()

    assert [r["action"] for r in result["results"]] == ["refreshed", "refreshed"]


async def test_refresh_touches_no_gate_on_a_no_op_tick(monkeypatch):
    shut: list[str] = []
    monkeypatch.setattr(harness_workflows, "get_harnesses", lambda: (_FakeHarness,))
    monkeypatch.setattr(harness_workflows, "HarnessConnection", _FakeConnections)
    monkeypatch.setattr(harness_workflows.gate, "shut", _fake_shut(shut, idle=True))
    _FakeHarness.due_connection_ids = set()
    _FakeHarness.urgent_connection_ids = set()

    await harness_workflows._refresh()

    assert shut == []
