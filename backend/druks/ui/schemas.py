from collections.abc import Iterable

from pydantic import Field, model_validator

from druks.schemas import Schema

from .blocks import Action, Block, Link, Watched


class Page(Schema):
    """One screen, as a page function projects it. The shared dashboard renders
    it, and rereads it on every snapshot of what ``follows`` watches."""

    title: str
    description: str = ""
    actions: list[Action | Link] = Field(default_factory=list)
    blocks: list[Block] = Field(default_factory=list)
    follows: Watched = None

    def __init__(self, title: str, **data):
        super().__init__(title=title, **data)

    @model_validator(mode="after")
    def _blocks_sit_where_they_work(self) -> "Page":
        regions: set[str] = set()
        for control in self.actions:
            control.check_placement(followed=bool(self.follows), regions=regions)
        for block in self.blocks:
            block.check_placement(followed=bool(self.follows), regions=regions)
        return self

    def iter_actions(self) -> "Iterable[Action]":
        for control in self.actions:
            yield from control.iter_actions()
        for block in self.blocks:
            yield from block.iter_actions()
