from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import ConfigDict

from druks.schemas import BaseResponse

if TYPE_CHECKING:
    from druks.services.base import Service
    from druks.services.models import OauthConnection, ServiceIdentity


# The settings-form field vocabulary (label/help/type), minus everything a
# write-only paste form has no use for (values, defaults, overrides).
class ServiceFieldSpec(BaseResponse):
    name: str
    label: str
    help: str
    type: str
    multiline: bool


class ConnectionResponse(BaseResponse):
    model_config = ConfigDict(from_attributes=True)

    id: str
    provider: str
    scopes: list[str]
    connected_at: datetime


class ServiceResponse(BaseResponse):
    # Connection state and identity facts only — never a stored secret.
    name: str
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
        row: "ServiceIdentity | None",
        connections: "list[OauthConnection] | None" = None,
    ) -> "ServiceResponse":
        return cls(
            name=service.name,
            title=service.title,
            description=service.description,
            required=service.required,
            connected=bool(row),
            facts=row.identity if row else {},
            connected_at=row.connected_at if row else None,
            fields=[ServiceFieldSpec(**spec) for spec in service.connect_fields()],
            is_oauth=bool(service.token_endpoint),
            required_scopes=list(service.required_scopes()),
            used_by=[declaration.label for declaration in service.declarations()],
            connections=[ConnectionResponse.model_validate(c) for c in connections or []],
        )
