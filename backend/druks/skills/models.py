from collections.abc import Collection
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship

from druks.core.models import Uuid7Pk
from druks.models import Base
from druks.skills.datastructures import InstalledSkill


class SkillCollection(Base, Uuid7Pk):
    __tablename__ = "skill_collections"

    name: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(String, unique=True)
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=Base.utc_now, onupdate=Base.utc_now)

    skills: Mapped[list["Skill"]] = relationship(
        back_populates="collection",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="Skill.name",
    )

    @classmethod
    async def list_all(cls, session: AsyncSession) -> list["SkillCollection"]:
        return list((await session.execute(select(cls).order_by(cls.name))).scalars())

    @classmethod
    async def get_for_source(cls, session: AsyncSession, source: str) -> "SkillCollection | None":
        result = await session.execute(select(cls).where(cls.source == source))
        return result.scalar_one_or_none()

    @classmethod
    async def create(
        cls, session: AsyncSession, *, source: str, name: str, skills: list[InstalledSkill]
    ) -> "SkillCollection":
        collection = cls(source=source, name=name)
        collection.skills = [
            Skill(
                name=skill.name,
                description=skill.description,
                path=skill.path,
                content_hash=skill.content_hash,
            )
            for skill in skills
        ]
        session.add(collection)
        await session.flush()
        return collection

    async def delete(self) -> None:
        await self.session.delete(self)
        await self.session.flush()


class Skill(Base, Uuid7Pk):
    __tablename__ = "skills"

    name: Mapped[str] = mapped_column(String, unique=True)
    description: Mapped[str] = mapped_column(String, default="")
    collection: Mapped[SkillCollection] = relationship(back_populates="skills", lazy="raise_on_sql")
    collection_id: Mapped[str] = mapped_column(ForeignKey("skill_collections.id"))
    # Disabled skills stay on disk but the delivery projection excludes them
    # from every sandbox upload.
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    path: Mapped[str] = mapped_column(String)
    content_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=Base.utc_now, onupdate=Base.utc_now)

    @classmethod
    async def installed_names(cls, session: AsyncSession) -> set[str]:
        return set((await session.execute(select(cls.name))).scalars())

    @classmethod
    async def list_enabled(cls, session: AsyncSession) -> list["Skill"]:
        # The operator's enabled catalog.
        stmt = select(cls).where(cls.enabled.is_(True)).order_by(cls.name)
        return list(await session.scalars(stmt))

    @classmethod
    async def list_delivered(
        cls, session: AsyncSession, requested: Collection[str]
    ) -> list["Skill"]:
        # What one call receives: the enabled skills it named, or the whole enabled
        # catalog when it named none.
        enabled = await cls.list_enabled(session)
        if requested:
            return [skill for skill in enabled if skill.name in requested]
        return enabled

    @classmethod
    async def delivery_excludes(
        cls, session: AsyncSession, requested: Collection[str]
    ) -> tuple[str, ...]:
        # Patterns are anchored to the skills_dir tar root (``-C skills_dir .``);
        # excluded skills remain installed on disk.
        delivered = {skill.name for skill in await cls.list_delivered(session, requested)}
        installed = await cls.installed_names(session)
        return tuple(f"./{name}" for name in sorted(installed - delivered))

    @classmethod
    async def get(cls, session: AsyncSession, name: str) -> "Skill | None":
        result = await session.execute(select(cls).where(cls.name == name))
        return result.scalar_one_or_none()
