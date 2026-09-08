from datetime import datetime
from typing import Any

from sqlalchemy import String, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from druks.database import db_session
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
    async def get(cls, provider: str) -> "ProviderCatalog | None":
        return await db_session().get(cls, provider)

    @classmethod
    async def list_all(cls) -> list["ProviderCatalog"]:
        return list(await db_session().scalars(select(cls).order_by(cls.provider)))

    @classmethod
    async def create(cls, provider: str, models: list[dict], *, label: str) -> "ProviderCatalog":
        session = db_session()
        row = await session.get(cls, provider)
        if not row:
            row = cls(provider=provider)
            session.add(row)
        row.models = models
        row.label = label
        row.fetched_at = Base.utc_now()
        await session.flush()
        return row

    async def delete(self) -> None:
        session = db_session()
        await session.delete(self)
        await session.flush()
