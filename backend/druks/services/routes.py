from fastapi import APIRouter, Depends, HTTPException

from druks.accounts.dependencies import current_session_account
from druks.extensions.registry import services
from druks.services.exceptions import ServiceConnectError, ServiceNotConnectedError
from druks.services.models import ServiceIdentity
from druks.services.schemas import ServiceResponse

router = APIRouter(prefix="/api/services", tags=["services"])


@router.get("", response_model=list[ServiceResponse], response_model_by_alias=True)
async def list_services() -> list[ServiceResponse]:
    entries = []
    for service in services.all():
        try:
            row = ServiceIdentity.get(service.name)
        except ServiceNotConnectedError:
            row = None
        entries.append(ServiceResponse.from_row(service, row))
    return entries


# Session identity only, like the settings PATCH: the appliance's own
# credentials are never writable with an agent PAT.
@router.post(
    "/{name}",
    response_model=ServiceResponse,
    response_model_by_alias=True,
    dependencies=[Depends(current_session_account)],
)
async def connect_service(name: str, payload: dict[str, str]) -> ServiceResponse:
    service = services.get(name)
    if not service:
        raise HTTPException(status_code=404, detail=f"No service {name!r}.")
    try:
        row = await service.connect(payload)
    except ServiceConnectError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return ServiceResponse.from_row(service, row)
