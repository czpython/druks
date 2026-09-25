from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import ConfigDict, Field

from druks.schemas import Schema

if TYPE_CHECKING:
    from druks.secrets.models import VaultSecret
    from druks.services.base import Service


# The settings-form field vocabulary (label/help/type), minus everything a
# write-only paste form has no use for (values, defaults, overrides).
class ServiceFieldSpec(Schema):
    name: str
    label: str
    help: str
    type: str
    multiline: bool


class ConnectionResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    id: str
    provider: str = Field(validation_alias="audience_name")
    scopes: list[str] | None
    identity: dict[str, Any]
    identity_status: str | None
    identity_error: str | None
    connected_at: datetime = Field(validation_alias="updated_at")
    revoked_at: datetime | None
    revoked_reason: str


class ServiceResponse(Schema):
    # Connection state and identity facts only — never a stored secret.
    slug: str
    title: str
    description: str
    required: bool
    connected: bool
    facts: dict[str, Any]
    connected_at: datetime | None
    fields: list[ServiceFieldSpec]
    is_oauth: bool
    scopes: list[str]
    used_by: list[str]
    connections: list[ConnectionResponse]

    @classmethod
    def from_row(
        cls,
        service: "type[Service]",
        row: "VaultSecret | None",
        connections: "list[VaultSecret] | None" = None,
    ) -> "ServiceResponse":
        return cls(
            slug=service.slug,
            title=service.title,
            description=service.description,
            required=service.required,
            connected=bool(row),
            facts=row.identity if row else {},
            connected_at=row.updated_at if row else None,
            fields=[ServiceFieldSpec(**spec) for spec in service.connect_fields()],
            is_oauth=bool(service.token_endpoint),
            scopes=list(service.scopes()),
            used_by=[declaration.label for declaration in service.declarations()],
            connections=connections or [],
        )
