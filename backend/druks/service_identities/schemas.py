from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from druks.schemas import BaseResponse

if TYPE_CHECKING:
    from druks.service_identities.models import ServiceIdentity


class GitHubConnectRequest(BaseModel):
    app_id: str = Field(validation_alias="appId")
    private_key: str = Field(validation_alias="privateKey")
    webhook_secret: str = Field(validation_alias="webhookSecret")


class GitHubIdentityResponse(BaseResponse):
    # Connection state only — identity facts, never either stored secret.
    connected: bool
    app_id: str | None
    slug: str | None
    connected_at: datetime | None

    @classmethod
    def from_row(cls, row: "ServiceIdentity | None") -> "GitHubIdentityResponse":
        facts = row.identity if row else {}
        return cls(
            connected=bool(row),
            app_id=facts.get("app_id"),
            slug=facts.get("slug"),
            connected_at=row.connected_at if row else None,
        )
