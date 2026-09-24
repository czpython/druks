import logging
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel, Field, SecretStr
from slack_sdk.errors import SlackApiError

from druks.core.apis.github import GitHubClient
from druks.core.apis.linear import LINEAR_GRAPHQL_URL
from druks.core.apis.slack import SLACK_AUTHORITY, SLACK_BOT_SCOPES, SlackClient
from druks.secrets.enums import SecretKind
from druks.services import Service, ServiceConnectError
from druks.settings import load_settings

logger = logging.getLogger(__name__)

_VERIFY_TIMEOUT = 10.0


class Github(Service):
    secret_kind = SecretKind.APP_KEY
    # The Drukbox catalog name a box holds this identity's token under.
    secret_name = "github"
    description = (
        "The GitHub App druks acts as. Create it from here, or paste an existing "
        "App's credentials from the GitHub developer settings page."
    )
    # What the created App is: the single operator identity documented in
    # docs/configuration.md — keep the two in step. The manifest flow adds the
    # appliance's own URLs before handing it to GitHub.
    manifest = {
        "name": "druks",
        "description": "Druks operator — receives webhooks, writes branches, PRs, and comments.",
        "public": False,
        "default_events": [
            "issue_comment",
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
        private_key: SecretStr = Field(
            title="Private key (PEM)", json_schema_extra={"multiline": True}
        )
        webhook_secret: SecretStr = Field(title="Webhook secret")

    @classmethod
    async def verify(cls, settings: Settings) -> dict[str, Any]:
        client = GitHubClient(
            app_id=settings.app_id,
            private_key=settings.private_key.get_secret_value(),
            base_url=load_settings().github_api_url,
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
            base_url=load_settings().github_api_url,
            slug=row.identity["slug"],
        )

    @classmethod
    async def issue_token(cls, resource: str) -> tuple[str, datetime]:
        """The installation token for the repo, and the expiry GitHub gave it."""
        return await (await cls.get_client()).token_for_repo(resource)


class Linear(Service):
    description = (
        "The Linear identity druks reads and updates tickets as; its webhook "
        "secret verifies inbound deliveries."
    )
    required = False

    class Settings(BaseModel):
        api_key: SecretStr = Field(title="API key")
        webhook_secret: SecretStr = Field(title="Webhook secret")

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

    class Settings(BaseModel):
        base_url: str = Field(title="Base URL", description="Base URL of the Jira Cloud site.")
        email: str = Field(title="Email")
        api_token: SecretStr = Field(title="API token")
        webhook_secret: SecretStr = Field(title="Webhook secret")

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
