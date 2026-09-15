from datetime import datetime

from sqlalchemy import Row, select
from sqlalchemy.ext.asyncio import AsyncSession

from druks.durable.models import AgentCall
from druks.secrets.models import VaultSecret


async def list_finished_calls(
    session: AsyncSession, account_id: str | None, *, since: datetime, until: datetime
) -> list[Row]:
    result = await session.execute(
        select(
            AgentCall.model,
            AgentCall.cost_usd,
            AgentCall.cost_metadata,
            AgentCall.finished_at,
        )
        .outerjoin(VaultSecret, AgentCall.subscription_id == VaultSecret.id)
        .where(VaultSecret.account_id == account_id)
        .where(AgentCall.finished_at.is_not(None))
        .where(AgentCall.finished_at >= since)
        .where(AgentCall.finished_at < until)
    )
    return list(result.all())
