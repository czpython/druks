from datetime import datetime
from typing import Annotated

from pydantic import BeforeValidator, ConfigDict, Field

from druks.schemas import Schema

# A set of names on the wire, in a stable order.
SortedNames = Annotated[list[str], BeforeValidator(sorted)]


class ProviderResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    id: str
    label: str
    login_kinds: SortedNames


class ProviderLoginResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    provider: str
    kind: str
    # The email the provider reported at connect — display, never authority.
    provider_email: str
    expires_at: datetime | None
    connected: bool = Field(validation_alias="is_connected")


class CatalogModel(Schema):
    id: str
    label: str


class ProviderCatalogResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    provider: str
    models: list[CatalogModel]
    fetched_at: datetime
