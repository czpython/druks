import json

import httpx2

from druks.accounts.enums import OperatorWrites
from druks.accounts.models import OperatorToken
from druks.core.models import uuid7_str
from druks.mcp.constants import PROPOSAL_PREFIX
from druks.redis import get_client
from druks.sandbox.constants import MAX_AGENT_TIMEOUT_SECONDS

_FAILURE_CHARS = 200


async def stash(run_id: str, write: dict[str, str]) -> None:
    """Record a write a deferred credential asked for, instead of performing it."""
    redis = get_client()
    key = f"{PROPOSAL_PREFIX}{run_id}"
    await redis.rpush(key, json.dumps(write))
    await redis.expire(key, MAX_AGENT_TIMEOUT_SECONDS)


async def take(run_id: str) -> list[dict[str, str]]:
    """The run's proposals, cleared as they are read, so one answer applies to
    one set."""
    redis = get_client()
    key = f"{PROPOSAL_PREFIX}{run_id}"
    items = await redis.lrange(key, 0, -1)
    await redis.delete(key)
    return [json.loads(item) for item in items]


async def play(account_id: str, writes: list[dict[str, str]]) -> list[str]:
    """Perform the proposals as that account, through a credential that dies
    with the replay. Returns one line per write that failed, so the caller can
    report it — one refusal does not stop the rest."""
    # A real cycle: the routes these writes hit are mounted on that app, and
    # they import this package.
    from druks.api.server import app

    call_id = uuid7_str()
    token = await OperatorToken.mint(
        account_id=account_id,
        agent_call_id=call_id,
        run_id=call_id,
        writes=OperatorWrites.ALLOW,
    )
    failures = []
    try:
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://druks",
        ) as client:
            for write in writes:
                answer = await client.request(
                    write["method"],
                    write["path"],
                    content=write["body"] or None,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": write["content_type"],
                    },
                )
                if answer.status_code >= 400:
                    failures.append(
                        f"{write['method']} {write['path']}: "
                        f"{answer.status_code} {answer.text[:_FAILURE_CHARS]}"
                    )
    finally:
        await OperatorToken.revoke(call_id)
    return failures
