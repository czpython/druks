from pydantic import Field, model_validator

from druks.schemas import BaseResponse

from .blocks import Block, Watched


class Page(BaseResponse):
    """One screen, as a page function projects it. The shared dashboard renders
    it, and rereads it on every snapshot of what ``follows`` watches."""

    title: str
    description: str = ""
    blocks: list[Block] = Field(default_factory=list)
    follows: Watched = None

    def __init__(self, title: str, **data):
        super().__init__(title=title, **data)

    @model_validator(mode="after")
    def _blocks_sit_where_they_work(self) -> "Page":
        regions: set[str] = set()
        for block in self.blocks:
            block.check_placement(followed=bool(self.follows), regions=regions)
        return self
