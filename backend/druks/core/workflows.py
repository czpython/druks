import logging

from druks.harnesses.datastructures import RotationResult
from druks.harnesses.models import HarnessConnection
from druks.harnesses.registry import get_harnesses
from druks.sandbox import gate
from druks.workflows import Workflow

logger = logging.getLogger(__name__)


class RefreshTokens(Workflow):
    every = "*/15 * * * *"

    async def run(self) -> dict[str, object]:
        # Every 15 min. With an ~8h Claude TTL refreshed at <2h remaining (and
        # codex ~10d at <24h), this keeps both tokens alive with a wide margin
        # while doing almost nothing on most ticks.
        return await _refresh()


async def _refresh() -> dict[str, object]:
    by_name = {harness.name: harness for harness in get_harnesses()}
    logins = [login for login in HarnessConnection.list_all() if login.harness in by_name]

    # rotate_token is the source of truth for what's due: it no-ops ("fresh", no
    # server call, no invalidation) any row outside its margin. A refresh
    # invalidates the old token server-side and would 401 a VM mid-run holding
    # a pushed copy, so a due rotation runs only while its login is idle —
    # shut the gate, rotate if no calls are active, else defer to the next
    # tick (the refresh margin dwarfs the call horizon). Once expiry is closer
    # than the horizon a mid-run 401 is unavoidable either way, so rotate
    # regardless. Other logins provision and run throughout; a no-op tick
    # touches no gate. Snapshot plain values before rotating: each refreshed
    # row commits as it lands, which expires every ORM object in the session
    # mid-loop.
    rows = [
        (
            login.harness,
            login.id,
            by_name[login.harness].needs_refresh(login),
            by_name[login.harness].refresh_is_urgent(login),
        )
        for login in logins
    ]

    results: list[RotationResult] = []
    for harness_name, login_id, due, urgent in rows:
        if due:
            async with gate.shut(login_id) as idle:
                if idle or urgent:
                    result = await by_name[harness_name].rotate_token(login_id)
                else:
                    result = RotationResult(harness_name, "busy", login_id=login_id)
        else:
            result = await by_name[harness_name].rotate_token(login_id)
        _log_result(result)
        results.append(result)

    return {
        "results": [
            {"harness": r.harness, "login_id": r.login_id, "action": r.action, "error": r.error}
            for r in results
        ],
    }


def _log_result(result: RotationResult) -> None:
    if result.action == "busy":
        logger.info(
            "deferring %s rotation for login %s; calls active", result.harness, result.login_id
        )
    elif result.action == "refreshed":
        logger.info(
            "refreshed %s token for login %s; expires_at=%s",
            result.harness,
            result.login_id,
            result.expires_at,
        )
    elif result.action == "failed" and result.error != "no_credentials":
        # invalid_grant => that login must reconnect; network/http_* => transient.
        # no_credentials is a row deleted mid-tick, not a failure — stay quiet.
        logger.warning(
            "token refresh failed for %s login %s: %s",
            result.harness,
            result.login_id,
            result.error,
        )
    elif result.action == "no_refresh_token":
        logger.warning(
            "%s login %s has no refresh token; cannot keep it alive",
            result.harness,
            result.login_id,
        )
    # "fresh" and "locked" (another worker owns this row's refresh) are quiet no-ops.
