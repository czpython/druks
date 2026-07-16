import asyncio

import druks.redis
import pytest
from druks.sandbox import gate

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    monkeypatch.setattr(gate, "_POLL", 0.01)
    druks.redis.get_client()._data.clear()
    druks.redis.get_client()._zsets.clear()
    yield


def _zset(login_id: str) -> dict[str, float]:
    return druks.redis.get_client()._zsets.get(f"druks:sandbox:active:{login_id}", {})


async def test_use_registers_the_call_for_its_span(db_session):
    async with gate.use("login-1", "call-1"):
        assert "call-1" in _zset("login-1")
    assert "call-1" not in _zset("login-1")


async def test_hold_waits_for_the_logins_active_calls(db_session):
    held: list[str] = []

    async def rotation():
        async with gate.hold("login-1"):
            held.append("rotated")

    async with gate.use("login-1", "call-1"):
        rotate = asyncio.create_task(rotation())
        await asyncio.sleep(0.05)
        assert not held  # the active call keeps the rotation waiting
    await asyncio.wait_for(rotate, timeout=2)
    assert held == ["rotated"]


async def test_other_logins_are_not_blocked(db_session):
    async with gate.hold("login-1"):
        # A different login registers and proceeds while login-1 rotates.
        async with asyncio.timeout(1):
            async with gate.use("login-2", "call-2"):
                assert "call-2" in _zset("login-2")


async def test_use_waits_out_a_rotation_of_its_login(db_session):
    entered: list[str] = []

    async def call():
        async with gate.use("login-1", "call-1"):
            entered.append("ran")

    async with gate.hold("login-1"):
        task = asyncio.create_task(call())
        await asyncio.sleep(0.05)
        assert not entered  # the shut gate keeps the call out
    await asyncio.wait_for(task, timeout=2)
    assert entered == ["ran"]


async def test_hold_prunes_expired_registrations(db_session):
    # A call that died unremoved has an expiry score in the past — rotation
    # must not wait on it.
    await druks.redis.get_client().zadd("druks:sandbox:active:login-1", {"dead-call": 1.0})
    async with asyncio.timeout(1):
        async with gate.hold("login-1"):
            pass
    assert "dead-call" not in _zset("login-1")
