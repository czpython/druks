from datetime import datetime
from typing import Annotated

from pydantic import BeforeValidator, ConfigDict, Field

from druks.accounts.schemas import AccountResponse
from druks.schemas import Schema

# A set of names on the wire, in a stable order.
SortedNames = Annotated[list[str], BeforeValidator(sorted)]


class ProviderResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    id: str
    label: str
    login_kinds: SortedNames


class ProviderSubscriptionResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    provider: str
    # The email the provider reported at connect — display, never authority.
    provider_email: str
    expires_at: datetime | None
    updated_at: datetime
    connected: bool = Field(validation_alias="is_connected")


class ProviderKeyResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    provider: str
    key_tail: str
    updated_by: AccountResponse
    updated_at: datetime


class CatalogModel(Schema):
    id: str
    label: str


class ProviderCatalogResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    provider: str
    models: list[CatalogModel]
    fetched_at: datetime
