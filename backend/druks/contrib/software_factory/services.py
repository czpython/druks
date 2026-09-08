from pydantic import BaseModel, Field, SecretStr

from druks.core.services import Github


class GithubReviewer(Github):
    """A second GitHub App. GitHub accepts its verdict reviews on pull requests
    the operator authored. Without it, reviews publish as operator comments."""

    required = False
    description = (
        "The GitHub App reviews post as. Paste an App with read access to metadata "
        "and contents, write access to pull requests, and no webhook. Leave it "
        "unconnected and reviews publish as operator comments."
    )

    class Settings(BaseModel):
        app_id: str = Field(title="App ID")
        private_key: SecretStr = Field(
            title="Private key (PEM)", json_schema_extra={"multiline": True}
        )
