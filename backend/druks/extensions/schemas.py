from druks.schemas import BaseResponse


class ExtensionResponse(BaseResponse):
    name: str
    icon: str
    description: str
    builtin: bool
    subject_types: list[str]
    has_frontend: bool
    # (url, name) pairs, straight from the extension's declaration.
    navigation: list[tuple[str, str]]
