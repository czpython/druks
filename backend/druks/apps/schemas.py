from druks.schemas import Schema


class PageEntry(Schema):
    name: str
    label: str
    path: str
    parent: str
    order: int


class Operation(Schema):
    id: str
    method: str
    path: str


class AppResponse(Schema):
    name: str
    icon: str
    description: str
    builtin: bool
    subject_types: list[str]
    has_frontend: bool
    # (url, label) pairs, one for each page the app's navigation names.
    navigation: list[tuple[str, str]]
    # In route-match order.
    pages: list[PageEntry]
    # The app's non-GET operations, which an Action names.
    operations: list[Operation]
