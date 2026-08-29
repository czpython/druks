from druks.schemas import BaseResponse


class AppResponse(BaseResponse):
    name: str
    icon: str
    description: str
    builtin: bool
    subject_types: list[str]
    has_frontend: bool
    # (url, label) pairs, one for each page the app's navigation names.
    navigation: list[tuple[str, str]]
