from datetime import datetime
from typing import TYPE_CHECKING, Any

from druks.schemas import BaseResponse

if TYPE_CHECKING:
    from druks.services.base import Service
    from druks.services.models import ServiceIdentity


# The settings-form field vocabulary (label/help/type), minus everything a
# write-only paste form has no use for (values, defaults, overrides).
class ServiceFieldSpec(BaseResponse):
    name: str
    label: str
    help: str
    type: str
    multiline: bool


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

    @classmethod
    def from_row(cls, service: "type[Service]", row: "ServiceIdentity | None") -> "ServiceResponse":
        return cls(
            name=service.name,
            title=service.title,
            description=service.description,
            required=service.required,
            connected=bool(row),
            facts=row.identity if row else {},
            connected_at=row.connected_at if row else None,
            fields=[ServiceFieldSpec(**spec) for spec in service.connect_fields()],
        )
