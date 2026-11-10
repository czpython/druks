import sqlalchemy
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship

from druks.accounts.enums import AccountKind
from druks.accounts.models import Account
from druks.apps.loader import iter_apps
from druks.core.models import Uuid7Pk
from druks.exceptions import ValidationError
from druks.models import Base, Model

team_members = sqlalchemy.Table(
    "team_members",
    Base.metadata,
    sqlalchemy.Column(
        "team_id", sqlalchemy.ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True
    ),
    sqlalchemy.Column(
        "account_id",
        sqlalchemy.ForeignKey("accounts.id", ondelete="RESTRICT"),
        primary_key=True,
    ),
)


class Team(Model, Uuid7Pk, ordering=("name",)):
    __tablename__ = "teams"

    name: Mapped[str] = mapped_column(unique=True)
    members: Mapped[list[Account]] = relationship(
        secondary=team_members,
        lazy="selectin",
        order_by=(Account.created_at, Account.id),
        passive_deletes=True,
    )
    apps: Mapped[list[str]] = mapped_column(JSONB, default=list)

    @classmethod
    async def list_all(cls, session: AsyncSession) -> list["Team"]:
        return list(await session.scalars(sqlalchemy.select(cls).order_by(cls.name)))

    @classmethod
    async def create(
        cls, session: AsyncSession, *, name: str, member_ids: list[str], apps: list[str]
    ) -> "Team":
        if await cls.get_or_none(session, name=name):
            raise ValidationError(f"A team named {name!r} already exists.")
        team = cls(
            name=name,
            members=await cls._get_members(session, member_ids),
            apps=cls._get_apps(apps),
        )
        session.add(team)
        await session.flush()
        return team

    async def replace(
        self, session: AsyncSession, *, name: str, member_ids: list[str], apps: list[str]
    ) -> None:
        """Set the name, the members and the apps."""
        other = await Team.get_or_none(session, name=name)
        if other and other.id != self.id:
            raise ValidationError(f"A team named {name!r} already exists.")
        self.name = name
        self.members = await self._get_members(session, member_ids)
        self.apps = self._get_apps(apps)
        await session.flush()

    @staticmethod
    async def _get_members(session: AsyncSession, member_ids: list[str]) -> list[Account]:
        """The operator accounts with these ids. Bot and bot admin accounts are refused."""
        operators = await session.scalars(
            sqlalchemy.select(Account).where(
                Account.id.in_(member_ids), Account.kind == AccountKind.OPERATOR
            )
        )
        members = {member.id: member for member in operators}
        for member_id in member_ids:
            if member_id not in members:
                raise ValidationError(f"Account {member_id!r} is not an operator account.")
        return list(members.values())

    @staticmethod
    def _get_apps(names: list[str]) -> list[str]:
        """The names, each one time. An app must be installed and not built in."""
        installed = {app.name: app for app in iter_apps()}
        for name in names:
            if name not in installed:
                raise ValidationError(f"App {name!r} is not installed.")
            if installed[name].builtin:
                raise ValidationError(f"App {name!r} is built in.")
        return list(dict.fromkeys(names))
