import asyncio
from contextlib import asynccontextmanager

import druks.redis
import pytest
from druks.core import workflows as harness_workflows
from druks.harnesses.datastructures import RotationResult
from druks.sandbox import gate


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    druks.redis.get_client()._data.clear()
    druks.redis.get_client()._zsets.clear()
    monkeypatch.setattr(gate, "_POLL", 0.01)
    yield


async def test_use_registers_for_the_span_and_unregisters():
    client = druks.redis.get_client()
    async with gate.use("login-1", "call-1"):
        assert await client.zcard("druks:sandbox:gate:users:login-1") == 1
    assert await client.zcard("druks:sandbox:gate:users:login-1") == 0


async def test_hold_waits_only_for_its_own_logins_calls():
    entered: list[str] = []

    async def rotate(login_id: str) -> None:
        async with gate.hold(login_id):
            entered.append(login_id)

    async with gate.use("login-1", "call-1"):
        # Another login rotates freely while login-1 has an active call.
        await asyncio.wait_for(rotate("login-2"), timeout=1.0)
        assert entered == ["login-2"]

        blocked = asyncio.create_task(rotate("login-1"))
        await asyncio.sleep(0.05)
        assert not blocked.done()  # waiting on login-1's active call
    await asyncio.wait_for(blocked, timeout=1.0)
    assert entered == ["login-2", "login-1"]


async def test_new_calls_wait_out_a_rotation_then_proceed():
    ran: list[str] = []

    async def call() -> None:
        async with gate.use("login-1", "call-9"):
            ran.append("call-9")

    async with gate.hold("login-1"):
        pending = asyncio.create_task(call())
        await asyncio.sleep(0.05)
        assert not pending.done()  # the rotating flag blocks registration
    await asyncio.wait_for(pending, timeout=1.0)
    assert ran == ["call-9"]


async def test_expired_registrations_never_wedge_a_rotation():
    # A crashed caller's registration ages out (score in the past) — the hold
    # prunes it and rotates instead of waiting the horizon.
    client = druks.redis.get_client()
    await client.zadd("druks:sandbox:gate:users:login-1", {"dead-call": 1.0})
    async with gate.hold("login-1"):
        assert await client.zcard("druks:sandbox:gate:users:login-1") == 0


class _FakeHarness:
    name = "fake"
    due_login_ids: set[str] = set()

    @classmethod
    def needs_refresh(cls, login):
        return login.id in cls.due_login_ids

    @classmethod
    async def rotate_token(cls, login_id):
        return RotationResult(cls.name, "refreshed", login_id=login_id)


class _FakeLogin:
    harness = "fake"

    def __init__(self, login_id: str) -> None:
        self.id = login_id


class _FakeLogins:
    @staticmethod
    def list_all():
        return [_FakeLogin("login-1"), _FakeLogin("login-2")]


async def test_refresh_holds_only_the_due_logins(monkeypatch):
    held: list[str] = []

    @asynccontextmanager
    async def fake_hold(login_id: str):
        held.append(login_id)
        yield

    monkeypatch.setattr(harness_workflows, "get_harnesses", lambda: (_FakeHarness,))
    monkeypatch.setattr(harness_workflows, "HarnessConnection", _FakeLogins)
    monkeypatch.setattr(harness_workflows.gate, "hold", fake_hold)
    _FakeHarness.due_login_ids = {"login-2"}

    result = await harness_workflows._refresh()

    # Only the due login's gate shut; every row still rotated (the fresh one
    # no-ops inside rotate_token itself).
    assert held == ["login-2"]
    assert [r["login_id"] for r in result["results"]] == ["login-1", "login-2"]


async def test_refresh_touches_no_gate_on_a_no_op_tick(monkeypatch):
    held: list[str] = []

    @asynccontextmanager
    async def fake_hold(login_id: str):
        held.append(login_id)
        yield

    monkeypatch.setattr(harness_workflows, "get_harnesses", lambda: (_FakeHarness,))
    monkeypatch.setattr(harness_workflows, "HarnessConnection", _FakeLogins)
    monkeypatch.setattr(harness_workflows.gate, "hold", fake_hold)
    _FakeHarness.due_login_ids = set()

    await harness_workflows._refresh()

    assert held == []
