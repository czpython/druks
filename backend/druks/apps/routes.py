from fastapi import APIRouter

from .loader import iter_apps
from .schemas import AppResponse, PageEntry

router = APIRouter(prefix="/api/apps", tags=["apps"])


@router.get("", response_model=list[AppResponse], response_model_by_alias=True)
async def list_apps() -> list[AppResponse]:
    roster = []
    for app in iter_apps():
        pages = app.pages()
        # The declaration counter runs across every app, so number it per app.
        declaration_order = {
            page.name: index
            for index, page in enumerate(sorted(pages, key=lambda page: page.order))
        }
        roster.append(
            AppResponse(
                name=app.name,
                icon=app.icon,
                description=app.description,
                builtin=app.builtin,
                subject_types=[subject.subject_type for subject in app.subjects()],
                has_frontend=bool(app.frontend_dist()),
                navigation=[
                    (f"/{app.name}{page.route}".rstrip("/"), page.label)
                    for page in app.navigation_pages()
                ],
                pages=[
                    PageEntry(
                        name=page.name,
                        label=page.label,
                        path=f"/{app.name}{page.route}".rstrip("/"),
                        parent=page.parent.name if page.parent else "",
                        order=declaration_order[page.name],
                    )
                    for page in pages
                ],
                operations=[
                    operation
                    for operation in app.operations().values()
                    if operation.method != "GET"
                ],
            )
        )
    return roster
