from fastapi import APIRouter, HTTPException

from druks.extensions.registry import services
from druks.services.exceptions import ServiceConnectError, ServiceNotConnectedError
from druks.services.models import ServiceIdentity
from druks.services.schemas import ServiceIdentityResponse

router = APIRouter(prefix="/api/service-identities", tags=["service-identities"])


@router.get("", response_model=list[ServiceIdentityResponse], response_model_by_alias=True)
async def list_service_identities() -> list[ServiceIdentityResponse]:
    entries = []
    for service in services.all():
        try:
            row = ServiceIdentity.get(service.name)
        except ServiceNotConnectedError:
            row = None
        entries.append(ServiceIdentityResponse.from_row(service, row))
    return entries


@router.post("/{name}", response_model=ServiceIdentityResponse, response_model_by_alias=True)
async def connect_service_identity(name: str, payload: dict[str, str]) -> ServiceIdentityResponse:
    service = services.get(name)
    if not service:
        raise HTTPException(status_code=404, detail=f"No service {name!r}.")
    try:
        row = await service.connect(payload)
    except ServiceConnectError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return ServiceIdentityResponse.from_row(service, row)
