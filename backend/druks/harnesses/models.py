from datetime import datetime
from typing import Any

from sqlalchemy import String, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from druks.models import Base


class ProviderCatalog(Base):
    """The models a provider offers, ``{"id", "label"}`` each with ids
    namespaced ``provider/model``; ``label`` names the provider."""

    __tablename__ = "provider_catalogs"

    provider: Mapped[str] = mapped_column(String, primary_key=True)
    label: Mapped[str]
    models: Mapped[Any] = mapped_column(JSONB)
    fetched_at: Mapped[datetime] = mapped_column(default=Base.utc_now, onupdate=Base.utc_now)

    @classmethod
    async def list_all(cls, session: AsyncSession) -> list["ProviderCatalog"]:
        return list(await session.scalars(select(cls).order_by(cls.provider)))

    @classmethod
    async def create(
        cls, session: AsyncSession, provider: str, models: list[dict], *, label: str
    ) -> "ProviderCatalog":
        row = await session.get(cls, provider)
        if not row:
            row = cls(provider=provider)
            session.add(row)
        row.models = models
        row.label = label
        row.fetched_at = Base.utc_now()
        await session.flush()
        return row

    async def delete(self, session: AsyncSession) -> None:
        await session.delete(self)
        await session.flush()
