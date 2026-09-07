from datetime import datetime

from sqlalchemy import Row, select

from druks.database import db_session
from druks.durable.models import AgentCall
from druks.harnesses.models import ProviderSubscription


async def list_finished_calls(
    account_id: str | None, *, since: datetime, until: datetime
) -> list[Row]:
    result = await db_session().execute(
        select(
            AgentCall.model,
            AgentCall.cost_usd,
            AgentCall.cost_metadata,
            AgentCall.finished_at,
        )
        .outerjoin(ProviderSubscription, AgentCall.subscription_id == ProviderSubscription.id)
        .where(ProviderSubscription.account_id == account_id)
        .where(AgentCall.finished_at.is_not(None))
        .where(AgentCall.finished_at >= since)
        .where(AgentCall.finished_at < until)
    )
    return list(result.all())
