from pydantic import Field

from druks.schemas import BaseResponse

from .blocks import Block


class Page(BaseResponse):
    """One screen, as a page function projects it. The shared dashboard
    renders it."""

    title: str
    description: str = ""
    blocks: list[Block] = Field(default_factory=list)
