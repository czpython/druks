import sqlalchemy
from sqlalchemy import ForeignKey, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from druks.accounts.models import Account
from druks.core.models import Uuid7Pk
from druks.exceptions import ObjectNotFound, PermissionDenied, ValidationError
from druks.harnesses.config import AgentConfig, build_config, check_config
from druks.harnesses.exceptions import AgentConfigError, HarnessNotConnectedError
from druks.harnesses.providers import provider_label
from druks.models import Model
from druks.skills.models import Skill
from druks.user_settings.models import InstallationSettings


class ConfiguredAgent(Model, Uuid7Pk):
    """An agent a person saves with its own instructions, skills, harness and model.
    A private agent shows only to its owner and the admins. A public agent shows to
    everyone."""

    __tablename__ = "agents"

    name: Mapped[str]
    description: Mapped[str]
    instructions: Mapped[str]
    skills: Mapped[list[str]] = mapped_column(JSONB)
    harness: Mapped[str]
    model: Mapped[str]
    owner_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    is_private: Mapped[bool]

    @classmethod
    async def list_for_account(
        cls, session: AsyncSession, account: Account
    ) -> list["ConfiguredAgent"]:
        return list(
            await session.scalars(select(cls).where(cls._for_account(account)).order_by(cls.name))
        )

    @classmethod
    async def get_for_account(
        cls, session: AsyncSession, agent_id: str, account: Account
    ) -> "ConfiguredAgent":
        agent = await session.scalar(
            select(cls).where(cls.id == agent_id, cls._for_account(account))
        )
        if agent:
            return agent
        raise ObjectNotFound("agent", {"id": agent_id})

    @classmethod
    async def get_for_name(cls, session: AsyncSession, name: str) -> "ConfiguredAgent | None":
        """The agent with this name in any case."""
        return await session.scalar(
            select(cls).where(sqlalchemy.func.lower(cls.name) == name.lower())
        )

    @classmethod
    async def get_for_text(cls, session: AsyncSession, text: str) -> "ConfiguredAgent | None":
        """The agent whose whole name starts the text, in any case. The longest such
        name wins: "Security engineer" over "Security"."""
        lower_text = sqlalchemy.func.lower(text)
        lower_name = sqlalchemy.func.lower(cls.name)
        # Both sides lower in Postgres: Python lowers "İ" to two characters, Postgres
        # to one, so a mix would never match.
        next_character = sqlalchemy.func.substr(
            lower_text, sqlalchemy.func.length(lower_name) + 1, 1
        )
        return await session.scalar(
            select(cls)
            .where(
                sqlalchemy.func.starts_with(lower_text, lower_name),
                # "Security engineers" does not name "Security engineer".
                next_character.op("!~")("[[:alnum:]]"),
            )
            .order_by(sqlalchemy.func.length(cls.name).desc())
            .limit(1)
        )

    @classmethod
    async def create(
        cls,
        session: AsyncSession,
        *,
        owner: Account,
        name: str,
        description: str,
        instructions: str,
        skills: list[str],
        harness: str,
        model: str,
        is_private: bool,
    ) -> "ConfiguredAgent":
        if await cls.get_for_name(session, name):
            raise ValidationError(f"An agent named {name!r} already exists.")
        await cls._check_harness(session, harness, model, is_private)
        agent = cls(
            name=name,
            description=description,
            instructions=instructions,
            skills=await cls._get_skills(session, skills),
            harness=harness,
            model=model,
            owner_id=owner.id,
            is_private=is_private,
        )
        session.add(agent)
        await session.flush()
        return agent

    async def replace(
        self,
        session: AsyncSession,
        *,
        editor: Account,
        owner_id: str,
        name: str,
        description: str,
        instructions: str,
        skills: list[str],
        harness: str,
        model: str,
        is_private: bool,
    ) -> None:
        """Set every field. Only an admin changes the owner."""
        self.check_editor(editor)
        if owner_id != self.owner_id and not editor.is_admin:
            raise PermissionDenied("Only an admin can change an agent's owner.")
        other = await ConfiguredAgent.get_for_name(session, name)
        if other and other.id != self.id:
            raise ValidationError(f"An agent named {name!r} already exists.")
        await self._check_harness(session, harness, model, is_private)
        self.owner_id = (await Account.get(session, id=owner_id)).id
        self.name = name
        self.description = description
        self.instructions = instructions
        self.skills = await self._get_skills(session, skills)
        self.harness = harness
        self.model = model
        self.is_private = is_private
        await session.flush()

    async def get_config(self, session: AsyncSession) -> AgentConfig:
        """A private agent runs on its owner's subscription. A public agent runs on the
        install's API key or, without one, the unattended account's subscription, so a
        turn never bills the person who asks."""
        if self.is_private:
            return await self._build_config(session, self.owner_id, "subscription")
        try:
            return await self._build_config(session, None, "api_key")
        except HarnessNotConnectedError as error:
            no_key = error
        try:
            return await self._build_config(session, None, "subscription")
        except AgentConfigError:
            # The API key attempt already checked that the harness runs the model, so a
            # config error here says only that the harness takes no subscription.
            raise no_key from None
        except HarnessNotConnectedError as error:
            label = await provider_label(session, self.model.partition("/")[0])
            raise HarnessNotConnectedError(
                f"add the {label} API key, or connect the unattended account's {label} "
                "subscription, in Settings → Providers."
            ) from error

    def check_editor(self, account: Account) -> None:
        """Only the owner or an admin changes or deletes an agent."""
        if account.id != self.owner_id and not account.is_admin:
            raise PermissionDenied("Only the agent's owner or an admin can change it.")

    def check_talker(self, account: Account) -> None:
        """Anyone talks to a public agent. Only the owner talks to their private agent:
        it runs on their subscription."""
        if self.is_private and account.id != self.owner_id:
            raise PermissionDenied("Only the agent's owner can talk to a private agent.")

    @classmethod
    def _for_account(cls, account: Account) -> sqlalchemy.ColumnElement[bool]:
        """The agents the account sees: an admin sees every agent."""
        if account.is_admin:
            return sqlalchemy.true()
        return sqlalchemy.or_(cls.is_private.is_(False), cls.owner_id == account.id)

    async def _build_config(
        self, session: AsyncSession, account_id: str | None, billing: str
    ) -> AgentConfig:
        """The agent's harness and model on this billing, with the installation's other
        execution defaults. No account id means the unattended account."""
        settings = await InstallationSettings.get_or_create(session)
        return await build_config(
            session,
            account_id,
            harness_name=self.harness,
            model=self.model,
            billing=billing,
            effort=settings.default_effort,
            timeout=settings.default_timeout,
            fast_mode=settings.fast_mode,
            include_apps=settings.include_apps,
        )

    @staticmethod
    async def _check_harness(
        session: AsyncSession, harness: str, model: str, is_private: bool
    ) -> None:
        """The harness must run the model. A private agent runs on its owner's
        subscription. A public agent can run on the install's API key, which every
        harness takes."""
        harness_class = await check_config(session, harness, model, "api_key")
        if is_private and "subscription" not in harness_class.billing_options:
            raise ValidationError(
                f"A private agent runs on its owner's subscription, and {harness} runs on an "
                "API key only. Pick another harness, or make the agent public."
            )

    @staticmethod
    async def _get_skills(session: AsyncSession, names: list[str]) -> list[str]:
        """The names, each one time. Each skill must be enabled in Settings → Skills."""
        enabled = {skill.name for skill in await Skill.list_enabled(session)}
        for name in names:
            if name not in enabled:
                raise ValidationError(
                    f"Skill {name!r} is not enabled. Enable it in Settings → Skills."
                )
        return list(dict.fromkeys(names))


sqlalchemy.Index("agents_name_lower_key", sqlalchemy.func.lower(ConfiguredAgent.name), unique=True)
