import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from redis.asyncio.lock import Lock
from redis.exceptions import RedisError

from druks.redis import get_client

from .constants import CHAT_LOCK_TTL_SECONDS


@asynccontextmanager
async def chat_lock(name: str) -> AsyncIterator[None]:
    """Hold a Redis lock for the whole block. The TTL stays short, so a dead process
    frees the lock soon, and the holder renews it, so a turn can outlast it."""
    lock = get_client().lock(
        name, timeout=CHAT_LOCK_TTL_SECONDS, sleep=0.25, raise_on_release_error=False
    )
    async with lock:
        renewal = asyncio.create_task(renew_lock(lock, asyncio.current_task()))
        try:
            yield
        finally:
            renewal.cancel()


async def renew_lock(lock: Lock, holder: asyncio.Task) -> None:
    try:
        while True:
            await asyncio.sleep(CHAT_LOCK_TTL_SECONDS / 3)
            await lock.reacquire()
    except RedisError:
        # The holder must stop: another process can own the lock now.
        holder.cancel()
