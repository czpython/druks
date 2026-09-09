from datetime import datetime
from typing import TYPE_CHECKING, Any

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
    id: str
    provider: str
    scopes: list[str]
    identity: dict[str, Any]
    connected_at: datetime
    revoked_at: datetime | None
    revoked_reason: str

    @classmethod
    def from_secret(cls, row: "VaultSecret") -> "ConnectionResponse":
        return cls(
            id=row.id,
            provider=row.audience_name,
            scopes=row.scopes,
            identity=row.identity,
            connected_at=row.updated_at,
            revoked_at=row.revoked_at,
            revoked_reason=row.revoked_reason,
        )


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
    required_scopes: list[str]
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
            required_scopes=list(service.required_scopes()),
            used_by=[declaration.label for declaration in service.declarations()],
            connections=[ConnectionResponse.from_secret(c) for c in connections or []],
        )
