import base64
import logging
import secrets
from contextlib import suppress
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx
from githubkit import GitHub
from pydantic import BaseModel, Field, SecretStr
from slack_sdk.errors import SlackApiError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from druks.accounts.models import Account
from druks.core.apis.exceptions import TwilioError, TwilioNotFoundError
from druks.core.apis.github import GITHUB_API_URL, GITHUB_AUTHORITY, GitHubClient
from druks.core.apis.linear import LINEAR_GRAPHQL_URL
from druks.core.apis.slack import SLACK_AUTHORITY, SLACK_BOT_SCOPES, SlackClient
from druks.core.apis.twilio import TwilioClient
from druks.secrets.datastructures import Audience
from druks.secrets.enums import SecretKind
from druks.secrets.models import VaultSecret
from druks.services import Service, ServiceConnectError
from druks.services.exceptions import ServiceNotConnectedError
from druks.settings import load_settings

logger = logging.getLogger(__name__)

_VERIFY_TIMEOUT = 10.0


class Github(Service):
    """The GitHub App that Druks acts as, and that people sign in to Druks through. A
    person's sign-in through it links their GitHub account to their Druks account."""

    secret_kind = SecretKind.APP_KEY
    # Drukbox knows this host: a token for it reaches git and gh.
    host = "github.com"
    description = (
        "The GitHub App druks acts as. Create it from here, or paste an existing "
        "App's credentials from the GitHub developer settings page."
    )
    authorization_endpoint = "https://github.com/login/oauth/authorize"
    token_endpoint = "https://github.com/login/oauth/access_token"
    mcp_host = "api.githubcopilot.com"
    # A fresh sign-in by the same GitHub user updates their row.
    identity_key = "subject"
    # What the created App is: the single operator identity documented in
    # docs/configuration.md — keep the two in step. The manifest flow adds the
    # appliance's own URLs before handing it to GitHub.
    manifest = {
        "name": "druks",
        "description": "Druks operator — receives webhooks, writes branches, PRs, and comments.",
        "public": False,
        "default_events": [
            "issue_comment",
            # Issue labelling is the GitHub tracker's intake signal.
            "issues",
            "pull_request",
            "pull_request_review",
            "pull_request_review_comment",
            "push",
        ],
        "default_permissions": {
            "metadata": "read",
            "contents": "write",
            # Generating a repo from a template is Administration, not Contents.
            "administration": "write",
            "pull_requests": "write",
            "issues": "write",
            "checks": "read",
            "statuses": "read",
        },
    }

    class Settings(BaseModel):
        app_id: str = Field(title="App ID")
        client_id: str = Field(title="Client ID")
        client_secret: SecretStr = Field(title="Client secret")
        private_key: SecretStr = Field(
            title="Private key (PEM)", json_schema_extra={"multiline": True}
        )
        webhook_secret: SecretStr = Field(title="Webhook secret")

    @classmethod
    def get_manifest(cls, *, endpoint: str, webhook_base: str) -> dict[str, Any]:
        """The App to create, for GitHub's create-from-manifest page."""
        return {
            **cls.manifest,
            "url": endpoint,
            "redirect_url": f"{endpoint}{cls.get_create_url()}/callback",
            "callback_urls": [f"{endpoint}/api/oauth/callback"],
            "hook_attributes": {"url": f"{webhook_base}/_external/github/events/", "active": True},
        }

    @classmethod
    async def verify(cls, settings: Settings) -> dict[str, Any]:
        client = GitHubClient(
            app_id=settings.app_id,
            private_key=settings.private_key.get_secret_value(),
        )
        try:
            slug = await client.get_authenticated_app_slug()
        except Exception as error:  # noqa: BLE001 — any auth/parse failure is a rejected paste
            logger.warning("GitHub service-identity connect rejected: %s", type(error).__name__)
            raise ServiceConnectError(
                "GitHub did not accept these credentials — check the App ID and PEM key."
            ) from error
        return {"slug": slug}

    @classmethod
    async def get_client(cls) -> GitHubClient:
        """The client of the connected App's vault row. PEM plaintext exists only
        here, feeding the auth strategy."""
        row = await cls.get()
        return GitHubClient(
            app_id=row.identity["app_id"],
            private_key=row.secrets["private_key"],
            app_url=row.identity.get("url", GITHUB_API_URL),
            slug=row.identity["slug"],
        )

    @classmethod
    async def get_install_endpoint(cls) -> str:
        slug = (await cls.get()).identity["slug"]
        return f"https://github.com/apps/{quote(slug, safe='')}/installations/new"

    @classmethod
    async def sync_installations(cls) -> None:
        """The accounts the App is installed on, as the fact ``installations``."""
        accounts = await (await cls.get_client()).list_installation_accounts(cached=False)
        card = await cls.get()
        card.identity = {**card.identity, "installations": list(accounts)}

    @classmethod
    async def issue_token(cls, resource: str) -> tuple[str, datetime]:
        """The installation token for the repo, and the expiry GitHub gave it."""
        return await (await cls.get_client()).token_for_repo(resource)

    @classmethod
    def is_grant_revoked(cls, status: int, tokens: dict[str, Any]) -> bool:
        # GitHub answers a dead refresh token with a 200.
        return tokens.get("error") == "bad_refresh_token"

    @classmethod
    async def get_identity(cls, access_token: str) -> dict[str, Any]:
        """The person behind a user token, keyed the way ``Account.lookup`` finds them."""
        async with GitHub(access_token, base_url=GITHUB_API_URL) as github:
            person = (await github.rest.users.async_get_authenticated()).parsed_data
        return {"authority": GITHUB_AUTHORITY, "subject": str(person.id), "login": person.login}


class Linear(Service):
    description = (
        "The Linear identity druks reads and updates tickets as; its webhook "
        "secret verifies inbound deliveries."
    )
    required = False
    mcp_host = "mcp.linear.app"

    class Settings(BaseModel):
        api_key: SecretStr = Field(title="API key")
        webhook_secret: SecretStr = Field(title="Webhook secret")

    @classmethod
    def get_authorization(cls, login: VaultSecret) -> str:
        return f"Bearer {login.secrets['api_key']}"

    @classmethod
    async def verify(cls, settings: Settings) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=_VERIFY_TIMEOUT) as client:
                response = await client.post(
                    LINEAR_GRAPHQL_URL,
                    json={"query": "{ viewer { name } organization { name } }"},
                    headers={"Authorization": settings.api_key.get_secret_value()},
                )
            response.raise_for_status()
            data = response.json()["data"]
            facts = {"actor": data["viewer"]["name"], "workspace": data["organization"]["name"]}
        except Exception as error:  # noqa: BLE001 — any auth/transport failure is a rejected paste
            logger.warning("Linear service-identity connect rejected: %s", type(error).__name__)
            raise ServiceConnectError("Linear did not accept this API key.") from error
        return facts


class Jira(Service):
    description = (
        "The Jira Cloud identity druks reads and updates tickets as; its webhook "
        "secret authenticates Automation deliveries."
    )
    required = False
    mcp_host = "mcp.atlassian.com"

    class Settings(BaseModel):
        base_url: str = Field(title="Base URL", description="Base URL of the Jira Cloud site.")
        email: str = Field(title="Email")
        api_token: SecretStr = Field(title="API token")
        webhook_secret: SecretStr = Field(title="Webhook secret")

    @classmethod
    def get_authorization(cls, login: VaultSecret) -> str:
        credentials = f"{login.identity['email']}:{login.secrets['api_token']}"
        return f"Basic {base64.b64encode(credentials.encode()).decode()}"

    @classmethod
    async def verify(cls, settings: Settings) -> dict[str, Any]:
        auth = httpx.BasicAuth(settings.email, settings.api_token.get_secret_value())
        try:
            async with httpx.AsyncClient(timeout=_VERIFY_TIMEOUT, auth=auth) as client:
                response = await client.get(f"{settings.base_url.rstrip('/')}/rest/api/3/myself")
            response.raise_for_status()
            facts = {"display_name": response.json()["displayName"]}
        except Exception as error:  # noqa: BLE001 — any auth/transport failure is a rejected paste
            logger.warning("Jira service-identity connect rejected: %s", type(error).__name__)
            raise ServiceConnectError(
                "Jira did not accept these credentials — check the base URL, email, and API token."
            ) from error
        return facts


class Slack(Service):
    """The Slack app that Druks answers as, in one workspace. The operator creates it
    from the manifest, installs it, and pastes its keys. People connect their own
    Slack account through the same app."""

    description = (
        "The Slack app that Druks answers as. Create it in Slack from the manifest, "
        "install it in your workspace, and paste its keys here."
    )
    required = False
    authorization_endpoint = "https://slack.com/oauth/v2/authorize"
    token_endpoint = "https://slack.com/api/oauth.v2.access"
    # Slack issues a person's token only with a scope. This one reads the identity.
    identity_scopes = ("users:read",)
    # A fresh sign-in by the same Slack user updates their row.
    identity_key = "subject"

    class Settings(BaseModel):
        client_id: str = Field(title="Client ID")
        client_secret: SecretStr = Field(title="Client secret")
        signing_secret: SecretStr = Field(title="Signing secret")
        bot_token: SecretStr = Field(title="Bot token")

    @classmethod
    async def verify(cls, settings: Settings) -> dict[str, Any]:
        try:
            bot = await SlackClient(token=settings.bot_token.get_secret_value()).auth_test()
        except SlackApiError as error:
            logger.warning("Slack connect rejected: %s", error)
            raise ServiceConnectError(
                "Slack did not accept the bot token. Paste the Bot User OAuth Token from the "
                "app's OAuth & Permissions page."
            ) from error
        return {
            "team": bot["team"],
            "team_id": bot["team_id"],
            "bot_name": bot["user"],
            "bot_user_id": bot["user_id"],
        }

    @classmethod
    async def get_identity(cls, access_token: str) -> dict[str, Any]:
        """The person behind a user token, keyed the way ``Account.lookup`` finds them."""
        person = await SlackClient(token=access_token).auth_test()
        return {
            "authority": SLACK_AUTHORITY.format(team_id=person["team_id"]),
            "subject": person["user_id"],
            "name": person["user"],
        }

    @classmethod
    def get_consent_query(cls, scopes: tuple[str, ...]) -> dict[str, str]:
        """Slack takes a person's scopes as ``user_scope``, joined by commas."""
        return {"user_scope": ",".join(scopes)}

    @classmethod
    def read_grant(cls, tokens: dict[str, Any]) -> dict[str, Any]:
        """A person's grant sits under ``authed_user``, with its scopes joined by commas.
        Token rotation stays off, as on every peer, so the access token never expires."""
        person = tokens["authed_user"]
        return {
            "access_token": person["access_token"],
            "refresh_token": "",
            "scopes": person["scope"].split(","),
        }

    @classmethod
    def get_manifest(cls) -> dict[str, Any]:
        """The app to create, for Slack's create-from-manifest page."""
        urls = load_settings().urls
        return {
            "display_information": {
                "name": "Druks",
                "description": "Your Druks agent, in Slack.",
            },
            "features": {
                "bot_user": {"display_name": "Druks", "always_online": True},
                # Without a writable Messages tab, Slack refuses every DM to the bot.
                "app_home": {"messages_tab_enabled": True, "messages_tab_read_only_enabled": False},
            },
            "oauth_config": {
                "redirect_urls": [f"{urls.endpoint.rstrip('/')}/api/oauth/callback"],
                "scopes": {"bot": list(SLACK_BOT_SCOPES), "user": list(cls.scopes())},
            },
            "settings": {
                "event_subscriptions": {
                    "request_url": f"{urls.webhook_base}/_external/slack/events/",
                    "bot_events": [
                        "message.channels",
                        "message.groups",
                        "message.im",
                        "message.mpim",
                    ],
                },
                "token_rotation_enabled": False,
            },
        }


class Twilio(Service):
    """The Twilio account that provides the phone numbers. Each linked number has its own
    connection."""

    description = "Your Twilio account. Druks answers calls on its phone numbers."
    required = False

    class Settings(BaseModel):
        account_sid: str = Field(title="Account SID")
        auth_token: SecretStr = Field(title="Auth token")

    @classmethod
    async def verify(cls, settings: Settings) -> dict:
        client = TwilioClient(settings.account_sid, settings.auth_token.get_secret_value())
        try:
            await client.get_account()
        except TwilioError as error:
            raise ServiceConnectError(
                "Twilio did not accept this account SID and auth token. Paste them from the "
                "Twilio Console."
            ) from error
        return {}

    @classmethod
    async def get_client(cls, session: AsyncSession) -> TwilioClient:
        if card := await VaultSecret.lookup(session, cls.secret_kind, Audience.service(cls.slug)):
            return TwilioClient(card.identity["account_sid"], card.secrets["auth_token"])
        raise ServiceNotConnectedError(cls.slug)

    @classmethod
    async def link(
        cls, session: AsyncSession, owner: Account, *, app: str, sid: str, calls_url: str
    ) -> VaultSecret:
        """Save the number with a new signing secret, then point its calls at ``calls_url``.
        If Twilio refuses the URL, the caller's rollback removes the row."""
        client = await cls.get_client(session)
        number = await client.get_number(sid)
        # A new row for each link, so a removed number keeps its calls.
        connection = VaultSecret(
            kind=SecretKind.SESSION,
            audience=Audience.service(cls.slug),
            account_id=owner.id,
            identity={"app": app, "number": number["phone_number"], "sid": sid},
            secrets={"signing_secret": secrets.token_urlsafe(32)},
        )
        session.add(connection)
        await session.flush()
        await client.set_voice_url(sid=sid, url=calls_url)
        return connection

    @classmethod
    async def unlink(cls, session: AsyncSession, connection: VaultSecret, reason: str) -> None:
        """Clear the number's voice URL, so its calls stop. The row stays, with its calls."""
        client = await cls.get_client(session)
        # The account may have released the number. Then there is no URL to clear.
        with suppress(TwilioNotFoundError):
            await client.set_voice_url(sid=connection.identity["sid"], url="")
        await connection.revoke(reason)

    @classmethod
    async def disconnect(cls, session: AsyncSession) -> None:
        """Unlink every number first. Clearing a voice URL needs the card's token."""
        for connection in await session.scalars(
            select(VaultSecret).where(
                VaultSecret.kind == SecretKind.SESSION,
                VaultSecret.audience == Audience.service(cls.slug),
                VaultSecret.revoked_at.is_(None),
            )
        ):
            await cls.unlink(session, connection, "service_disconnected")
        await super().disconnect(session)

    @classmethod
    async def list_connections(cls, session: AsyncSession, app: str) -> list[VaultSecret]:
        """An app's numbers, removed ones included."""
        return list(
            await session.scalars(
                select(VaultSecret)
                .where(
                    VaultSecret.kind == SecretKind.SESSION,
                    VaultSecret.audience == Audience.service(cls.slug),
                    VaultSecret.identity["app"].astext == app,
                )
                .order_by(VaultSecret.created_at, VaultSecret.id)
            )
        )

    @classmethod
    async def get_for_number(cls, session: AsyncSession, number: str) -> VaultSecret | None:
        """The live connection of a phone number."""
        return await session.scalar(
            select(VaultSecret).where(
                VaultSecret.kind == SecretKind.SESSION,
                VaultSecret.audience == Audience.service(cls.slug),
                VaultSecret.revoked_at.is_(None),
                VaultSecret.identity["number"].astext == number,
            )
        )

    @classmethod
    async def get_connection(cls, session: AsyncSession, connection_id: str) -> VaultSecret | None:
        """A number's connection, live or removed."""
        connection = await session.get(VaultSecret, connection_id)
        if (
            connection
            and connection.kind == SecretKind.SESSION
            and connection.audience == Audience.service(cls.slug)
        ):
            return connection
        return

    @classmethod
    async def list_linked_sids(cls, session: AsyncSession) -> set[str]:
        """The Twilio SIDs of the numbers that are linked now."""
        return set(
            await session.scalars(
                select(VaultSecret.identity["sid"].astext).where(
                    VaultSecret.kind == SecretKind.SESSION,
                    VaultSecret.audience == Audience.service(cls.slug),
                    VaultSecret.revoked_at.is_(None),
                )
            )
        )

    @classmethod
    async def list_unlinked_numbers(cls, session: AsyncSession) -> list[dict]:
        """The account's numbers that take calls and are not linked yet."""
        linked_sids = await cls.list_linked_sids(session)
        client = await cls.get_client(session)
        return [
            number for number in await client.list_numbers() if number["sid"] not in linked_sids
        ]
