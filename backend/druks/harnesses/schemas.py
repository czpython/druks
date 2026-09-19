from datetime import datetime
from typing import Annotated

from pydantic import AliasPath, BeforeValidator, ConfigDict, Field

from druks.accounts.schemas import AccountResponse
from druks.schemas import Schema

SortedNames = Annotated[list[str], BeforeValidator(sorted)]


class ProviderResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    id: str
    label: str
    billing_options: SortedNames


class ProviderSubscriptionResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    provider: str = Field(validation_alias="audience_name")
    # Display only. Druks never authorizes by it.
    provider_email: str = Field(validation_alias=AliasPath("identity", "email"))
    expires_at: datetime | None
    updated_at: datetime
    last_refreshed_at: datetime | None
    revoked_at: datetime | None
    revoked_reason: str
    connected: bool = Field(validation_alias="is_connected")


class ProviderKeyResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    provider: str = Field(validation_alias="audience_name")
    key_tail: str
    updated_by: AccountResponse | None = Field(validation_alias="pasted_by")
    updated_at: datetime


class CatalogModel(Schema):
    id: str
    label: str


class ProviderCatalogResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    provider: str
    label: str
    models: list[CatalogModel]
    fetched_at: datetime


class ProviderDirectoryResponse(Schema):
    provider: str
    label: str
    documentation_url: str | None
    api_url: str | None
    models: list[CatalogModel]
