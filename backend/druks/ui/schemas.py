from druks.schemas import BaseResponse


class Page(BaseResponse):
    """One screen, as a page function projects it. The shared dashboard
    renders it."""

    title: str
    description: str = ""
