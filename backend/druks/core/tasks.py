import logging

from druks.harnesses.datastructures import RotationResult
from druks.harnesses.models import HarnessConnection
from druks.harnesses.registry import get_harnesses
from druks.sandbox import gate
from druks.user_settings.models import HarnessSettings, UserSettings
from druks.workflows import task

logger = logging.getLogger(__name__)


@task(every="*/15 * * * *")
async def refresh_tokens() -> None:
    # Every 15 min. With an ~8h Claude TTL refreshed at <2h remaining (and
    # codex ~10d at <24h), this keeps both tokens alive with a wide margin
    # while doing almost nothing on most ticks.
    await _refresh()


@task(every="0 6 * * *")
async def refresh_models() -> None:
    fallback_id = UserSettings.get().fallback_account_id
    for harness in get_harnesses():
        connections = HarnessConnection.list_for_harness(harness.name)
        if not connections:
            continue
        preferred = [c for c in connections if c.account_id == fallback_id]
        settings = HarnessSettings.require(harness.name)
        await settings.refresh_models((preferred or connections)[0])


async def _refresh() -> dict[str, object]:
    by_name = {harness.name: harness for harness in get_harnesses()}
    connections = [c for c in HarnessConnection.list_all() if c.harness in by_name]

    # A refresh 401s a VM mid-call holding the old token, so a due rotation
    # runs only while its connection is idle — busy defers to the next tick;
    # urgent rotates regardless. rotate_token no-ops rows outside their
    # margin. Snapshot plain values: each refresh commits and expires the
    # session's ORM objects mid-loop.
    rows = [
        (
            connection.harness,
            connection.id,
            by_name[connection.harness].needs_refresh(connection),
            by_name[connection.harness].refresh_is_urgent(connection),
        )
        for connection in connections
    ]

    results: list[RotationResult] = []
    for harness_name, connection_id, due, urgent in rows:
        if due:
            async with gate.shut(connection_id) as idle:
                if idle or urgent:
                    result = await by_name[harness_name].rotate_token(connection_id)
                else:
                    result = RotationResult(harness_name, "busy", connection_id=connection_id)
        else:
            result = await by_name[harness_name].rotate_token(connection_id)
        _log_result(result)
        results.append(result)

    return {
        "results": [
            {
                "harness": r.harness,
                "connection_id": r.connection_id,
                "action": r.action,
                "error": r.error,
            }
            for r in results
        ],
    }


def _log_result(result: RotationResult) -> None:
    if result.action == "busy":
        logger.info(
            "deferring %s rotation for login %s; calls active", result.harness, result.connection_id
        )
    elif result.action == "refreshed":
        logger.info(
            "refreshed %s token for login %s; expires_at=%s",
            result.harness,
            result.connection_id,
            result.expires_at,
        )
    elif result.action == "failed" and result.error != "no_credentials":
        # invalid_grant => that login must reconnect; network/http_* => transient.
        # no_credentials is a row deleted mid-tick, not a failure — stay quiet.
        logger.warning(
            "token refresh failed for %s login %s: %s",
            result.harness,
            result.connection_id,
            result.error,
        )
    elif result.action == "no_refresh_token":
        logger.warning(
            "%s login %s has no refresh token; cannot keep it alive",
            result.harness,
            result.connection_id,
        )
    # "fresh" and "locked" (another worker owns this row's refresh) are quiet no-ops.
