import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from druks.redis import get_client

from .constants import GATE_USERS_PREFIX, MAX_AGENT_TIMEOUT_SECONDS, ROTATING_PREFIX

# One gate per credential: rotation runs only while its connection is idle
# (busy defers to the next tick); other connections never block. Active calls
# register in a zset scored by expiry, so a crashed caller ages out.
_RUN_HORIZON = MAX_AGENT_TIMEOUT_SECONDS  # a sandbox run never outlives this; caps every wait
_POLL = 2.0
# The gate is only shut for the seconds a refresh takes; a short TTL means a
# crashed holder frees the login fast instead of blocking it for the horizon.
_SHUT_TTL_SECONDS = 60


@asynccontextmanager
async def use(connection_id: str, call_id: str) -> AsyncIterator[None]:
    """Register the call as an active user of its connection for its span."""
    client = get_client()
    rotating = f"{ROTATING_PREFIX}{connection_id}"
    users = f"{GATE_USERS_PREFIX}{connection_id}"
    while True:
        waited = 0.0
        while waited < _RUN_HORIZON and await client.exists(rotating):
            await asyncio.sleep(_POLL)
            waited += _POLL
        await client.zadd(users, {call_id: time.time() + _RUN_HORIZON})
        # A flag landing between the wait and the add must not race the
        # rotation: back out and re-wait.
        if not await client.exists(rotating):
            break
        await client.zrem(users, call_id)
    try:
        yield
    finally:
        await client.zrem(users, call_id)


@asynccontextmanager
async def shut(connection_id: str) -> AsyncIterator[bool]:
    """Shut the connection's gate; yield True when idle — rotate now — else
    defer to the next tick. Reopens on exit either way."""
    client = get_client()
    rotating = f"{ROTATING_PREFIX}{connection_id}"
    users = f"{GATE_USERS_PREFIX}{connection_id}"
    await client.set(rotating, "1", ex=_SHUT_TTL_SECONDS)
    try:
        await client.zremrangebyscore(users, "-inf", time.time())
        yield not await client.zcard(users)
    finally:
        await client.delete(rotating)
