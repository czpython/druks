from fastapi import APIRouter

from .loader import iter_apps
from .schemas import AppResponse

router = APIRouter(prefix="/api/apps", tags=["apps"])


@router.get("", response_model=list[AppResponse], response_model_by_alias=True)
async def list_apps() -> list[AppResponse]:
    return [
        AppResponse(
            name=app.name,
            icon=app.icon,
            description=app.description,
            builtin=app.builtin,
            subject_types=[subject.subject_type for subject in app.subjects()],
            has_frontend=bool(app.frontend_dist()),
            navigation=app.navigation,
        )
        for app in iter_apps()
    ]
