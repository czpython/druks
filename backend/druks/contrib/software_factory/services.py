from typing import Any

from pydantic import BaseModel, Field, SecretStr

from druks.core.services import Github


class GithubReviewer(Github):
    """A second GitHub App. GitHub accepts its verdict reviews on pull requests
    the operator authored. Without it, reviews publish as operator comments."""

    required = False
    description = (
        "Optional: a second GitHub App, so reviews can approve pull requests. Without it, "
        "reviews publish as comments from the operator App. "
        "[Read the setup guide](https://docs.druks.ai/configuration#review-identity-optional)."
    )
    # Nobody signs in through the reviewer App, and no MCP server rides it: it only posts.
    authorization_endpoint = ""
    token_endpoint = ""
    mcp_host = ""
    # docs/configuration.md lists these permissions — keep the two in step.
    manifest = {
        "name": "druks-reviewer",
        "description": "Druks reviewer — approves pull requests and requests changes.",
        "public": False,
        "default_permissions": {
            "metadata": "read",
            "contents": "read",
            "pull_requests": "write",
        },
    }

    class Settings(BaseModel):
        app_id: str = Field(title="App ID")
        private_key: SecretStr = Field(
            title="Private key (PEM)", json_schema_extra={"multiline": True}
        )

    @classmethod
    def get_manifest(cls, *, endpoint: str, webhook_base: str) -> dict[str, Any]:
        # No sign-in callback and no webhook: the reviewer receives nothing.
        return {
            **cls.manifest,
            "url": endpoint,
            "redirect_url": f"{endpoint}{cls.get_create_url()}/callback",
        }
