import asyncio

import pytest
from druks.exceptions import LockLostError
from druks.locks import lock
from druks.redis import get_client
from redis.asyncio.lock import Lock
from redis.exceptions import ConnectionError as RedisConnectionError


async def test_a_second_holder_waits_for_the_first():
    entered = asyncio.Event()

    async def second():
        async with lock("lock-test"):
            entered.set()

    async with lock("lock-test"):
        task = asyncio.create_task(second())
        await asyncio.sleep(0.05)
        assert not entered.is_set()
    await asyncio.wait_for(task, 2)
    assert entered.is_set()


async def test_the_holder_renews_the_lock_past_its_ttl(monkeypatch):
    monkeypatch.setattr("druks.locks._TTL_SECONDS", 0.5)

    async with lock("lock-test"):
        await asyncio.sleep(1)
        assert await get_client().exists("lock-test")


async def test_one_failed_renewal_keeps_the_holder(monkeypatch):
    monkeypatch.setattr("druks.locks._TTL_SECONDS", 0.5)
    reacquire = Lock.reacquire
    failures = [RedisConnectionError()]

    async def fail_once(redis_lock):
        if failures:
            raise failures.pop()
        return await reacquire(redis_lock)

    monkeypatch.setattr(Lock, "reacquire", fail_once)

    async with lock("lock-test"):
        await asyncio.sleep(1)
        assert await get_client().exists("lock-test")


async def test_a_holder_that_loses_the_lock_stops(monkeypatch):
    monkeypatch.setattr("druks.locks._TTL_SECONDS", 0.5)

    with pytest.raises(LockLostError):
        async with lock("lock-test"):
            await get_client().delete("lock-test")
            await asyncio.sleep(1)
