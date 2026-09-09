from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated

from pydantic import BeforeValidator, ConfigDict

from druks.accounts.schemas import AccountResponse
from druks.schemas import Schema

# A set of names on the wire, in a stable order.
SortedNames = Annotated[list[str], BeforeValidator(sorted)]


if TYPE_CHECKING:
    from druks.accounts.models import Account
    from druks.secrets.models import VaultSecret


class ProviderResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    id: str
    label: str
    billing_options: SortedNames


class ProviderSubscriptionResponse(Schema):
    provider: str
    # The email the provider reported at connect — display, never authority.
    provider_email: str
    expires_at: datetime | None
    updated_at: datetime
    # False once the token has expired.
    connected: bool

    @classmethod
    def from_secret(cls, row: "VaultSecret") -> "ProviderSubscriptionResponse":
        return cls(
            provider=row.audience_name,
            provider_email=row.identity["email"],
            expires_at=row.expires_at,
            updated_at=row.updated_at,
            connected=row.is_live and (not row.expires_at or row.expires_at > datetime.now(UTC)),
        )


class ProviderKeyResponse(Schema):
    provider: str
    key_tail: str
    updated_by: AccountResponse | None
    updated_at: datetime

    @classmethod
    def from_secret(cls, row: "VaultSecret", pasted_by: "Account | None") -> "ProviderKeyResponse":
        return cls(
            provider=row.audience_name,
            key_tail=row.secrets["value"][-4:],
            updated_by=AccountResponse.model_validate(pasted_by) if pasted_by else None,
            updated_at=row.updated_at,
        )


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
