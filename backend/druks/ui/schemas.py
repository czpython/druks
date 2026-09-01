from collections.abc import Iterable

from pydantic import Field, model_validator

from druks.schemas import Schema

from .blocks import Action, Block, Watched


class Page(Schema):
    """One screen, as a page function projects it. The shared dashboard renders
    it, and rereads it on every snapshot of what ``follows`` watches."""

    title: str
    description: str = ""
    action: Action | None = None
    blocks: list[Block] = Field(default_factory=list)
    follows: Watched = None

    def __init__(self, title: str, **data):
        super().__init__(title=title, **data)

    @model_validator(mode="after")
    def _blocks_sit_where_they_work(self) -> "Page":
        regions: set[str] = set()
        if self.action:
            self.action.check_placement(followed=bool(self.follows), regions=regions)
        for block in self.blocks:
            block.check_placement(followed=bool(self.follows), regions=regions)
        return self

    def iter_actions(self) -> "Iterable[Action]":
        if self.action:
            yield self.action
        for block in self.blocks:
            yield from block.iter_actions()
