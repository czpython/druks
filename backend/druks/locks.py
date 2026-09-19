import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import cast

from redis.asyncio.lock import Lock
from redis.exceptions import LockNotOwnedError, RedisError

from druks.exceptions import LockHeldError, LockLostError
from druks.redis import get_client

_TTL_SECONDS = 30


@asynccontextmanager
async def lock(name: str, *, blocking: bool = True) -> AsyncIterator[None]:
    """Hold the named lock across all processes for the whole block. The holder renews
    a short TTL, so a long block keeps the lock and a dead process frees it soon. Raises
    LockHeldError at once with blocking=False, and LockLostError if the holder loses the lock."""
    redis_lock = get_client().lock(name, timeout=_TTL_SECONDS, sleep=0.25, blocking=blocking)
    if not await redis_lock.acquire():
        raise LockHeldError(name)
    holder = cast(asyncio.Task, asyncio.current_task())
    renewal = asyncio.create_task(renew_lock(redis_lock, holder))
    try:
        yield
    except asyncio.CancelledError:
        # The renewal ends only when it cancels the holder. DBOS records an error as
        # the run's failure, and it records nothing for a cancelled task.
        if renewal.done():
            holder.uncancel()
            raise LockLostError(name) from None
        raise
    finally:
        renewal.cancel()
        with suppress(LockNotOwnedError):
            await redis_lock.release()


async def renew_lock(redis_lock: Lock, holder: asyncio.Task) -> None:
    while True:
        await asyncio.sleep(_TTL_SECONDS / 3)
        try:
            await redis_lock.reacquire()
        except LockNotOwnedError:
            # The holder must stop: another process can own the lock now.
            holder.cancel()
            return
        except RedisError:
            # The lock stays with the holder until the TTL ends. The next renewal tries again.
            continue
