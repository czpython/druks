import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from druks.redis import get_client

from .constants import MAX_AGENT_TIMEOUT_SECONDS

_RUN_HORIZON = MAX_AGENT_TIMEOUT_SECONDS  # an agent call never outlives this; caps every wait
_POLL = 2.0


def _rotating_key(login_id: str) -> str:
    return f"druks:sandbox:rotating:{login_id}"


def _active_key(login_id: str) -> str:
    return f"druks:sandbox:active:{login_id}"


@asynccontextmanager
async def use(login_id: str, call_id: str) -> AsyncIterator[None]:
    """Register one agent call as an active user of the login for its span —
    provisioning through execution. A rotation of that login waits for the
    call; other logins' rotations don't."""
    client = get_client()
    while True:
        await _wait_until_open(login_id)
        # Scores are expiry stamps: a member whose call died unremoved falls
        # out of hold()'s prune instead of blocking rotation forever.
        await client.zadd(_active_key(login_id), {call_id: time.time() + _RUN_HORIZON})
        # Re-check after registering: a rotation that shut the gate between the
        # wait and the zadd must not run concurrently with this call.
        if not await client.exists(_rotating_key(login_id)):
            break
        await client.zrem(_active_key(login_id), call_id)
    try:
        yield
    finally:
        await client.zrem(_active_key(login_id), call_id)


@asynccontextmanager
async def hold(login_id: str) -> AsyncIterator[None]:
    """Shut the login's gate for a rotation: no new calls register, and the
    rotation waits for the login's active calls to finish (expired
    registrations are pruned). Other logins keep provisioning throughout."""
    client = get_client()
    await client.set(_rotating_key(login_id), "1", ex=int(_RUN_HORIZON))
    try:
        await _drain(login_id)
        yield
    finally:
        await client.delete(_rotating_key(login_id))


async def _wait_until_open(login_id: str) -> None:
    client = get_client()
    waited = 0.0
    while waited < _RUN_HORIZON and await client.exists(_rotating_key(login_id)):
        await asyncio.sleep(_POLL)
        waited += _POLL


async def _drain(login_id: str) -> None:
    client = get_client()
    key = _active_key(login_id)
    waited = 0.0
    while waited < _RUN_HORIZON:
        await client.zremrangebyscore(key, "-inf", time.time())
        if not await client.zcard(key):
            return
        await asyncio.sleep(_POLL)
        waited += _POLL
