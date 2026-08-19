from fastapi import APIRouter

from .loader import iter_extensions
from .schemas import ExtensionResponse

router = APIRouter(prefix="/api/extensions", tags=["extensions"])


@router.get("", response_model=list[ExtensionResponse], response_model_by_alias=True)
async def list_extensions() -> list[ExtensionResponse]:
    return [
        ExtensionResponse(
            name=extension.name,
            icon=extension.icon,
            description=extension.description,
            builtin=extension.builtin,
            subject_types=[subject.subject_type for subject in extension.subjects()],
            has_frontend=bool(extension.frontend_dist()),
            navigation=extension.navigation,
        )
        for extension in iter_extensions()
    ]
