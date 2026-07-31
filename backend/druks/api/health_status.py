from datetime import UTC, datetime, timedelta

from druks.api import schemas as api_schemas
from druks.core.utils.time import operator_local_day
from druks.durable import AgentCall
from druks.user_settings.models import UserSettings
from druks.webhooks.deliveries import last_delivery_at


def _spend_for_local_today(*, timezone_name: str, now: datetime) -> tuple[float, int]:
    _, local_start = operator_local_day(timezone_name, now)
    return AgentCall.total_run_spend_between(
        start=local_start.astimezone(UTC),
        end=(local_start + timedelta(days=1)).astimezone(UTC),
    )


async def build_health() -> api_schemas.DashboardHealth:
    now = datetime.now(UTC)
    spend, tokens = _spend_for_local_today(timezone_name=UserSettings.get().timezone, now=now)
    # GitHub is the code host and is always present.
    sources = ["github"]
    return api_schemas.DashboardHealth(
        web="ok",
        webhook_freshness=api_schemas.WebhookFreshness(
            sources=[
                api_schemas.WebhookSource(source=source, last_at=await last_delivery_at(source))
                for source in sources
            ],
        ),
        spend_today_usd=spend,
        tokens_today=tokens,
    )
