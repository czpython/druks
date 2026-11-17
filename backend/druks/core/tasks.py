import logging
from zoneinfo import ZoneInfo

from dbos import EnqueueOptions

from druks.db import db_session
from druks.durable.constants import NOTIFICATIONS_QUEUE, STALE_PARKED_FAILURE
from druks.durable.engine import enqueue_in_transaction
from druks.durable.models import Run
from druks.files.storage import reap_deleted_file_bytes
from druks.harnesses.constants import SIGN_IN_END_WARNING
from druks.harnesses.datastructures import RotationResult
from druks.harnesses.directory import refresh_added_catalogs
from druks.harnesses.providers import get_provider, get_providers
from druks.models import Base
from druks.notifications.constants import SEND_WORKFLOW
from druks.notifications.models import Notification
from druks.sandbox import gate
from druks.sandbox.client import sandbox_client
from druks.sandbox.models import SandboxIdentity
from druks.secrets.models import VaultSecret
from druks.settings import get_settings
from druks.usage.models import UsageScrape
from druks.workflows import task

logger = logging.getLogger(__name__)


# Scheduled here because @task registration is app-scoped and core is the
# platform's app; the files concern owns the mechanics.
@task(every="0 * * * *")
async def reap_deleted_files() -> None:
    await reap_deleted_file_bytes(db_session())


@task(every="*/15 * * * *")
async def refresh_tokens() -> None:
    # Fifteen minutes keeps every token inside its refresh margin and does
    # almost nothing on most ticks.
    await _refresh()


@task(every="*/5 * * * *")
async def refresh_usage() -> None:
    for subscription in await VaultSecret.list_subscriptions(db_session()):
        provider = get_provider(subscription.audience_name)
        if provider.has_usage(subscription) and await UsageScrape.is_due(
            db_session(), subscription, now=Base.utc_now()
        ):
            await provider.poll_usage(db_session(), subscription)


@task(every="0 * * * *")
async def warn_ending_sign_ins() -> None:
    now = Base.utc_now()
    endpoint = get_settings().urls.endpoint.rstrip("/")
    for subscription in await VaultSecret.list_subscriptions(db_session()):
        ends_at = subscription.refresh_token_expires_at
        # No warning once the end has passed: the sign-in cannot be saved by then.
        if (
            ends_at
            and not subscription.sign_in_end_warned_at
            and now < ends_at <= now + SIGN_IN_END_WARNING
        ):
            subscription.sign_in_end_warned_at = now
            label = get_provider(subscription.audience_name).label
            local_end = ends_at.astimezone(ZoneInfo(subscription.account.timezone))
            notification = await Notification.create(
                db_session(),
                account_id=subscription.account_id,
                reason="subscription.sign_in_ending",
                body=(
                    f"Your {label} sign-in ends on {local_end:%d %b %Y at %H:%M %Z}. "
                    "Reconnect it in Settings → Providers before then."
                ),
                deep_link=f"{endpoint}/settings/providers" if endpoint else None,
            )
            # The notice and its mark commit with the task's session, so neither goes alone.
            options: EnqueueOptions = {
                "workflow_name": SEND_WORKFLOW,
                "queue_name": NOTIFICATIONS_QUEUE,
                "workflow_id": f"{SEND_WORKFLOW}:{notification.id}",
            }
            await enqueue_in_transaction(db_session(), options, notification.id)


@task(every="0 * * * *")
async def release_orphan_boxes() -> None:
    await _release_orphan_boxes()


async def _release_orphan_boxes() -> None:
    # A run that died without its cleanup leaves its box until the lease ends.
    for identity in await SandboxIdentity.list_orphans(db_session()):
        logger.info("releasing the orphan box %s of run %s", identity.host_id, identity.run_id)
        await sandbox_client.release(host_id=identity.host_id)


@task(every="0 * * * *")
async def cancel_stale_parked_runs() -> None:
    # A stale run holds its subject's slot, and no reply can answer it.
    for run in await Run.list_stale_parked(db_session()):
        logger.info(
            "cancelling run %s: its ask on gate %s expired at %s",
            run.id,
            run.input_gate,
            run.input_expires_at,
        )
        await run.cancel(failure=STALE_PARKED_FAILURE)


@task(every="0 6 * * *")
async def refresh_catalogs() -> None:
    for provider in get_providers():
        await provider.refresh_catalog(db_session())
    await refresh_added_catalogs(db_session())


async def _refresh() -> dict[str, object]:
    subscriptions = await VaultSecret.list_subscriptions(db_session())

    # A rotation ends the token every box holds, so a due one waits for an
    # idle subscription unless it is urgent. Plain values: each refresh
    # commits and expires the session's rows.
    rows = [
        (
            subscription.audience_name,
            subscription.id,
            get_provider(subscription.audience_name).needs_refresh(subscription),
            get_provider(subscription.audience_name).refresh_is_urgent(subscription),
        )
        for subscription in subscriptions
    ]

    results: list[RotationResult] = []
    for provider_id, subscription_id, is_due, is_urgent in rows:
        provider = get_provider(provider_id)
        if is_due:
            async with gate.shut(subscription_id) as is_idle:
                if is_idle or is_urgent:
                    result = await provider.rotate_token(db_session(), subscription_id)
                else:
                    result = RotationResult(provider_id, "busy", subscription_id=subscription_id)
        else:
            result = await provider.rotate_token(db_session(), subscription_id)
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
