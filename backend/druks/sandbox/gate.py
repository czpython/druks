import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from druks.redis import get_client

from .constants import MAX_AGENT_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

# One gate per credential: a login's token rotation waits only for that
# login's active agent calls, and every other login keeps provisioning and
# running throughout. Active users live in a sorted set scored by their expiry
# (capped at the agent horizon), so a crashed caller ages out instead of
# holding the gate forever. Idle warm VMs never block a rotation — every
# invocation rewrites credentials fresh.
_RUN_HORIZON = MAX_AGENT_TIMEOUT_SECONDS  # a sandbox run never outlives this; caps every wait
_POLL = 2.0


def _rotating_key(login_id: str) -> str:
    return f"druks:sandbox:rotating:{login_id}"


def _users_key(login_id: str) -> str:
    return f"druks:sandbox:gate:users:{login_id}"


@asynccontextmanager
async def use(login_id: str, call_id: str) -> AsyncIterator[None]:
    """Register one agent call as an active user of its login, provisioning
    through execution. Registration re-checks the rotating flag after adding
    itself and backs out if it appeared — the flag may land between the check
    and the add, and staying registered would deadlock that rotation."""
    client = get_client()
    while True:
        waited = 0.0
        while waited < _RUN_HORIZON and await client.exists(_rotating_key(login_id)):
            await asyncio.sleep(_POLL)
            waited += _POLL
        await client.zadd(_users_key(login_id), {call_id: time.time() + _RUN_HORIZON})
        if not await client.exists(_rotating_key(login_id)):
            break
        await client.zrem(_users_key(login_id), call_id)
    try:
        yield
    finally:
        await client.zrem(_users_key(login_id), call_id)


@asynccontextmanager
async def hold(login_id: str) -> AsyncIterator[None]:
    """Shut one login's gate for its token rotation: block that login's new
    calls, wait out its active ones (expired registrations pruned first),
    rotate, release."""
    client = get_client()
    await client.set(_rotating_key(login_id), "1", ex=int(_RUN_HORIZON))
    try:
        waited = 0.0
        while True:
            await client.zremrangebyscore(_users_key(login_id), "-inf", time.time())
            if not await client.zcard(_users_key(login_id)):
                break
            if waited >= _RUN_HORIZON:
                logger.warning(
                    "gate for login %s hit the horizon with calls still registered; "
                    "rotating anyway",
                    login_id,
                )
                break
            await asyncio.sleep(_POLL)
            waited += _POLL
        yield
    finally:
        await client.delete(_rotating_key(login_id))
