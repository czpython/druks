import logging
from typing import Any

import httpx
from pydantic import BaseModel, Field, SecretStr

from druks.core.apis.github import GITHUB, GitHubClient
from druks.services import Service, ServiceConnectError
from druks.settings import load_settings

logger = logging.getLogger(__name__)

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"

_VERIFY_TIMEOUT = 10.0


class GitHubApp(Service):
    name = GITHUB
    title = "GitHub"
    description = (
        "The GitHub App druks acts as — it receives webhooks and writes branches, "
        "pull requests, and comments. Create it from here, or paste an existing "
        "App's credentials from the GitHub developer settings page."
    )
    # What the created App is: the single operator identity documented in
    # docs/configuration.md — keep the two in step. The manifest flow adds the
    # appliance's own URLs before handing it to GitHub.
    manifest = {
        "name": "druks",
        "description": "Druks operator — receives webhooks, writes branches, PRs, and comments.",
        "public": False,
        "default_events": ["issue_comment", "pull_request", "pull_request_review", "push"],
        "default_permissions": {
            "metadata": "read",
            "contents": "write",
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


class Linear(Service):
    name = "linear"
    title = "Linear"
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
    name = "jira"
    title = "Jira"
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
