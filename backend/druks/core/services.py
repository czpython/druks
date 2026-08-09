import logging
from typing import Any

from pydantic import BaseModel, Field, SecretStr

from druks.core.apis.github import GITHUB, GitHubClient
from druks.services import Service, ServiceConnectError
from druks.settings import load_settings

logger = logging.getLogger(__name__)


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
