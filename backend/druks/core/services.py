import base64
import logging
import secrets
from contextlib import suppress
from dataclasses import replace
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
from druks.core.apis.exceptions import GitHubAppNotInstalledError, TwilioError, TwilioNotFoundError
from druks.core.apis.github import GITHUB_API_URL, GITHUB_AUTHORITY, GitHubClient
from druks.core.apis.jira import SERVICE_ACCOUNT_DOMAIN, JiraClient
from druks.core.apis.linear import LINEAR_AUTHORITY, LINEAR_GRAPHQL_URL, LinearClient
from druks.core.apis.slack import SLACK_AUTHORITY, SLACK_BOT_SCOPES, SlackClient
from druks.core.apis.twilio import TwilioClient
from druks.secrets.datastructures import Audience
from druks.secrets.enums import SecretKind
from druks.secrets.exceptions import MissingSecretError
from druks.secrets.models import VaultSecret
from druks.services import Service, ServiceConnectError, auth
from druks.services.exceptions import OauthExchangeError, ServiceNotConnectedError
from druks.settings import load_settings

logger = logging.getLogger(__name__)

_VERIFY_TIMEOUT = 10.0


class GithubApp(Service):
    """A GitHub App that Druks acts as. Its private key mints an installation token per
    repo, and GitHub's page for the App installs it on an account."""

    abstract = True
    # Drukbox knows this host: a token for it reaches git and gh.
    host = "github.com"

    class Settings(BaseModel):
        app_id: str = Field(title="App ID")

    class Authentication(auth.Authentication):
        jwt = auth.Jwt()
        # The App installs through the sign-in door. People sign in through it only where
        # a service names the identity key, as ``Github`` does.
        sign_in = auth.SignIn(
            authorization_endpoint="https://github.com/login/oauth/authorize",
            token_endpoint="https://github.com/login/oauth/access_token",
        )

        @classmethod
        async def get_install_endpoint(cls) -> str:
            row = await cls.service.get()
            return (
                f"https://github.com/apps/{quote(row.identity['slug'], safe='')}/installations/new"
            )

        @classmethod
        async def complete_install(cls, tokens: dict[str, Any]) -> None:
            client = await cls.service.get_client()
            accounts = await client.list_installation_accounts(cached=False)
            row = await cls.service.get()
            row.identity = {**row.identity, "installations": list(accounts)}

        @classmethod
        def is_grant_revoked(cls, response: httpx.Response) -> bool:
            # GitHub answers a dead refresh token with a 200.
            return response.json().get("error") == "bad_refresh_token"

        @classmethod
        async def issue_token(
            cls, resources: tuple[str, ...] = (), *, name: str = ""
        ) -> tuple[str, datetime | None]:
            if not resources:
                raise MissingSecretError(
                    "GitHub issues a token for repositories. This entry names none."
                )
            client = await cls.service.get_client()
            try:
                return await client.get_token_for_repos(*resources)
            except GitHubAppNotInstalledError as error:
                raise MissingSecretError(str(error)) from error

    @classmethod
    def get_manifest(cls, *, endpoint: str, webhook_base: str) -> dict[str, Any]:
        """The App to create, for GitHub's create-from-manifest page. The install returns
        through the sign-in door, so every App names it."""
        return {
            **cls.manifest,
            "url": endpoint,
            "redirect_url": f"{endpoint}{cls.get_create_url()}/callback",
            "callback_urls": [f"{endpoint}/api/oauth/callback"],
            # GitHub sends the installer back through the sign-in door with a code.
            "request_oauth_on_install": True,
        }

    @classmethod
    async def verify(cls, settings: BaseModel) -> dict[str, Any]:
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


class Github(GithubApp):
    """The GitHub App that Druks acts as, and that people sign in to Druks through. A
    person's sign-in through it links their GitHub account to their Druks account."""

    description = (
        "The GitHub App druks acts as. Create it from here, or paste an existing "
        "App's credentials from the GitHub developer settings page."
    )
    mcp_host = "api.githubcopilot.com"

    class Settings(GithubApp.Settings):
        webhook_secret: SecretStr = Field(title="Webhook secret")

    class Authentication(GithubApp.Authentication):
        # A fresh sign-in by the same GitHub user updates their row.
        sign_in = replace(GithubApp.Authentication.sign_in, identity_key="subject")

        @classmethod
        async def get_identity(cls, access_token: str) -> dict[str, Any]:
            """The person behind a user token, keyed the way ``Account.lookup`` finds them."""
            async with GitHub(access_token, base_url=GITHUB_API_URL) as github:
                person = (await github.rest.users.async_get_authenticated()).parsed_data
            return {
                "authority": GITHUB_AUTHORITY,
                "subject": str(person.id),
                "login": person.login,
            }

    # What the created App is: the single operator identity documented in
    # docs/configuration.md — keep the two in step.
    manifest = {
        "name": "druks",
        "description": (
            "Druks receives repository events and writes branches, pull requests, and comments."
        ),
        "public": False,
        "default_events": [
            "check_run",
            "check_suite",
            "issue_comment",
            "issues",
            "pull_request",
            "pull_request_review",
            "pull_request_review_comment",
            "push",
            "repository",
            "status",
            "workflow_run",
        ],
        "default_permissions": {
            "metadata": "read",
            "contents": "write",
            "pull_requests": "write",
            "issues": "write",
            "checks": "write",
            "statuses": "write",
            "workflows": "write",
            "actions": "write",
            "repository_creation": "write",
            "administration": "read",
            "discussions": "read",
            "vulnerability_alerts": "read",
            "security_events": "read",
            "members": "read",
            "emails": "read",
        },
    }

    @classmethod
    def get_manifest(cls, *, endpoint: str, webhook_base: str) -> dict[str, Any]:
        return {
            **super().get_manifest(endpoint=endpoint, webhook_base=webhook_base),
            "hook_attributes": {"url": f"{webhook_base}/_external/github/events/", "active": True},
        }


class Linear(Service):
    description = (
        "The Linear identity druks reads and updates tickets as; its webhook "
        "secret verifies inbound deliveries."
    )
    required = False
    mcp_host = "mcp.linear.app"

    class Settings(BaseModel):
        webhook_secret: SecretStr = Field(title="Webhook secret")

    class Authentication(auth.Authentication):
        api_key = auth.ApiKey()
        oauth = auth.Oauth(
            authorization_endpoint="https://linear.app/oauth/authorize",
            token_endpoint="https://api.linear.app/oauth/token",
            # A fresh sign-in by the same Linear user updates their row.
            identity_key="subject",
            # The app installs as itself; a person signs in as themselves.
            install_params={"actor": "app", "scope": "read,write"},
        )

        @classmethod
        def get_consent_query(cls, scopes: tuple[str, ...]) -> dict[str, str]:
            """Linear joins scopes with commas."""
            return {"scope": ",".join(scopes)}

        @classmethod
        async def get_identity(cls, access_token: str) -> dict[str, Any]:
            """The person behind a user token, keyed the way ``Account.lookup`` finds them."""
            async with httpx.AsyncClient(timeout=_VERIFY_TIMEOUT) as client:
                response = await client.post(
                    LINEAR_GRAPHQL_URL,
                    json={"query": "{ viewer { id name email } }"},
                    headers={"Authorization": f"Bearer {access_token}"},
                )
            response.raise_for_status()
            viewer = response.json()["data"]["viewer"]
            return {
                "authority": LINEAR_AUTHORITY,
                "subject": viewer["id"],
                "name": viewer["name"],
                "email": viewer["email"],
            }

        @classmethod
        def is_grant_revoked(cls, response: httpx.Response) -> bool:
            # The manager refuses the refresh of an installation that Linear revoked.
            return response.status_code != 200 and response.json().get("error") in (
                "invalid_grant",
                "access_denied",
            )

    @classmethod
    async def get_client(cls) -> LinearClient:
        row = await cls.get()
        if "api_key" in row.secrets:
            return LinearClient(authorization=row.secrets["api_key"])
        return LinearClient(authorization=f"Bearer {await cls.get_access_token()}")

    @classmethod
    async def verify(cls, settings: BaseModel) -> dict[str, Any]:
        facts = {}
        if settings.api_key:
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
        "secret authenticates Automation deliveries. The API key is an Atlassian API token."
    )
    required = False
    mcp_host = "mcp.atlassian.com"

    class Settings(BaseModel):
        base_url: str = Field(title="Base URL", description="Base URL of the Jira Cloud site.")
        email: str = Field(
            title="Email",
            description=(
                "The Atlassian account that owns the token. A service account's is its "
                f"address at {SERVICE_ACCOUNT_DOMAIN}."
            ),
        )
        webhook_secret: SecretStr = Field(title="Webhook secret")

    class Authentication(auth.Authentication):
        api_key = auth.ApiKey()

        @classmethod
        def get_authorization(cls, login: VaultSecret) -> str:
            # Atlassian's MCP server takes a person's token as Basic auth and a service
            # account's key as a bearer token.
            email = login.identity["email"]
            token = login.secrets["api_key"]
            if email.endswith(f"@{SERVICE_ACCOUNT_DOMAIN}"):
                return f"Bearer {token}"
            return f"Basic {base64.b64encode(f'{email}:{token}'.encode()).decode()}"

    @classmethod
    async def verify(cls, settings: BaseModel) -> dict[str, Any]:
        token = settings.api_key.get_secret_value()
        basic_auth = httpx.BasicAuth(settings.email, token)
        try:
            async with httpx.AsyncClient(timeout=_VERIFY_TIMEOUT, auth=basic_auth) as wire:
                client = JiraClient(
                    base_url=settings.base_url, email=settings.email, api_key=token, client=wire
                )
                facts = {"display_name": (await client.get_myself())["displayName"]}
        except Exception as error:  # noqa: BLE001 — any auth/transport failure is a rejected paste
            logger.warning("Jira service-identity connect rejected: %s", type(error).__name__)
            raise ServiceConnectError(
                "Jira did not accept these credentials — check the base URL, email, and API token."
            ) from error
        return facts


class Slack(Service):
    description = "The Slack app that Druks answers as, in one workspace."
    required = False

    class Settings(BaseModel):
        signing_secret: SecretStr = Field(title="Signing secret")

    class Authentication(auth.Authentication):
        oauth = auth.Oauth(
            authorization_endpoint="https://slack.com/oauth/v2/authorize",
            token_endpoint="https://slack.com/api/oauth.v2.access",
            # Slack issues a person's token only with a scope. This one reads the identity.
            identity_scopes=("users:read",),
            # A fresh sign-in by the same Slack user updates their row.
            identity_key="subject",
            install_params={"scope": ",".join(SLACK_BOT_SCOPES)},
        )

        @classmethod
        async def complete_install(cls, tokens: dict[str, Any]) -> None:
            cls._check_answer(tokens)
            try:
                bot = await SlackClient(token=tokens["access_token"]).auth_test()
            except SlackApiError as error:
                raise OauthExchangeError(
                    cls.service.slug,
                    "Slack did not accept the new bot token. Install the app again.",
                    context={},
                ) from error
            row = await cls.service.get()
            row.identity = {
                **row.identity,
                "team": bot["team"],
                "team_id": bot["team_id"],
                "bot_name": bot["user"],
                "bot_user_id": bot["user_id"],
            }

        @classmethod
        async def get_identity(cls, access_token: str) -> dict[str, Any]:
            """The person behind a user token, keyed the way ``Account.lookup`` finds them."""
            person = await SlackClient(token=access_token).auth_test()
            row = await cls.service.get()
            if person["team_id"] != row.identity.get("team_id"):
                raise OauthExchangeError(
                    cls.service.slug,
                    "the Slack account is in another workspace. Connect your account in the "
                    "workspace that Druks answers in.",
                    context={},
                )
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
            cls._check_answer(tokens)
            person = tokens["authed_user"]
            return {
                "access_token": person["access_token"],
                "refresh_token": "",
                "scopes": person["scope"].split(","),
            }

        @classmethod
        def _check_answer(cls, tokens: dict[str, Any]) -> None:
            # Slack answers a refused exchange with HTTP 200 and ``ok`` false.
            if not tokens.get("ok"):
                raise OauthExchangeError(
                    cls.service.slug,
                    f"Slack answered {tokens.get('error')}. Try again.",
                    context={},
                )

    @classmethod
    async def get_bot_token(cls) -> str:
        """The bot token the install granted."""
        return await cls.get_access_token()

    @classmethod
    async def get_client(cls) -> SlackClient:
        return SlackClient(token=await cls.get_bot_token())

    @classmethod
    def get_manifest(cls) -> dict[str, Any]:
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
                "interactivity": {
                    "is_enabled": True,
                    "request_url": f"{urls.webhook_base}/_external/slack/interactivity/",
                },
                "token_rotation_enabled": False,
            },
        }


class Twilio(Service):
    """The Twilio account that provides the phone numbers. Each linked number has its own
    connection."""

    description = (
        "Your Twilio account. Druks answers calls on its phone numbers. The API key is the "
        "Auth token in the Twilio Console."
    )
    required = False

    class Settings(BaseModel):
        account_sid: str = Field(title="Account SID")

    class Authentication(auth.Authentication):
        api_key = auth.ApiKey()

    @classmethod
    async def verify(cls, settings: BaseModel) -> dict:
        client = TwilioClient(settings.account_sid, settings.api_key.get_secret_value())
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
        if row := await VaultSecret.lookup(session, cls.secret_kind, Audience.service(cls.slug)):
            return TwilioClient(row.identity["account_sid"], row.secrets["api_key"])
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
        """Unlink every number first. Clearing a voice URL needs the account's auth token."""
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
