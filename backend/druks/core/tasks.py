import logging

from druks.files.storage import reap_deleted_file_bytes
from druks.harnesses.datastructures import RotationResult
from druks.harnesses.directory import refresh_added_catalogs
from druks.harnesses.models import ProviderSubscription
from druks.harnesses.providers import get_provider, get_providers
from druks.sandbox import gate
from druks.workflows import task

logger = logging.getLogger(__name__)


# Scheduled here because @task registration is app-scoped and core is the
# platform's app; the files concern owns the mechanics.
@task(every="0 * * * *")
async def reap_deleted_files() -> None:
    await reap_deleted_file_bytes()


@task(every="*/15 * * * *")
async def refresh_tokens() -> None:
    # Every 15 min. With an ~8h Claude TTL refreshed at <2h remaining (and
    # codex ~10d at <24h), this keeps both tokens alive with a wide margin
    # while doing almost nothing on most ticks.
    await _refresh()


@task(every="0 6 * * *")
async def refresh_catalogs() -> None:
    for provider in get_providers():
        await provider.refresh_catalog()
    await refresh_added_catalogs()


async def _refresh() -> dict[str, object]:
    subscriptions = await ProviderSubscription.list_all()

    # A refresh 401s a VM mid-call holding the old token, so a due rotation
    # runs only while its subscription is idle — busy defers to the next tick;
    # urgent rotates regardless. rotate_token no-ops rows outside their
    # margin. Snapshot plain values: each refresh commits and expires the
    # session's ORM objects mid-loop.
    rows = [
        (
            subscription.provider,
            subscription.id,
            get_provider(subscription.provider).needs_refresh(subscription),
            get_provider(subscription.provider).refresh_is_urgent(subscription),
        )
        for subscription in subscriptions
    ]

    results: list[RotationResult] = []
    for provider_id, subscription_id, is_due, is_urgent in rows:
        provider = get_provider(provider_id)
        if is_due:
            async with gate.shut(subscription_id) as is_idle:
                if is_idle or is_urgent:
                    result = await provider.rotate_token(subscription_id)
                else:
                    result = RotationResult(provider_id, "busy", subscription_id=subscription_id)
        else:
            result = await provider.rotate_token(subscription_id)
        _log_result(result)
        results.append(result)

    return {
        "results": [
            {
                "provider": r.provider,
                "subscription_id": r.subscription_id,
                "action": r.action,
                "error": r.error,
            }
            for r in results
        ],
    }


def _log_result(result: RotationResult) -> None:
    if result.action == "busy":
        logger.info(
            "deferring %s rotation for subscription %s; calls active",
            result.provider,
            result.subscription_id,
        )
    elif result.action == "refreshed":
        logger.info(
            "refreshed %s token for subscription %s; expires_at=%s",
            result.provider,
            result.subscription_id,
            result.expires_at,
        )
    elif result.action == "failed" and result.error != "no_credentials":
        # invalid_grant => that subscription must reconnect; network/http_* => transient.
        # no_credentials is a row deleted mid-tick, not a failure — stay quiet.
        logger.warning(
            "token refresh failed for %s subscription %s: %s",
            result.provider,
            result.subscription_id,
            result.error,
        )
    elif result.action == "no_refresh_token":
        logger.warning(
            "%s subscription %s has no refresh token; cannot keep it alive",
            result.provider,
            result.subscription_id,
        )
    # "fresh" and "locked" (another worker owns this row's refresh) are quiet no-ops.
