from collections.abc import Iterator
from typing import TypeVar

from pydantic import Field, model_validator

from druks.schemas import Schema

from .blocks import Action, Block, Columns, Link, Watched
from .fields import Field as PageField

Part = TypeVar("Part", bound=Schema)


class Page(Schema):
    """One screen, as a page function projects it. The shared dashboard renders
    it, and rereads it on every snapshot of what ``follows`` watches."""

    title: str
    description: str = ""
    controls: list[Action | Link] = Field(default_factory=list)
    filters: list[PageField] = Field(default_factory=list)
    blocks: list[Block] = Field(default_factory=list)
    follows: Watched = None

    def __init__(self, title: str, **data):
        super().__init__(title=title, **data)

    @model_validator(mode="after")
    def _blocks_sit_where_they_work(self) -> "Page":
        regions: set[str] = set()
        for control in self.controls:
            control.check_placement(followed=bool(self.follows), regions=regions)
        for block in self.blocks:
            block.check_placement(followed=bool(self.follows), regions=regions)
        splits = [part for part in self.iter_parts(Columns) if part.layout == "split"]
        sole = self.blocks[0] if len(self.blocks) == 1 else None
        if splits and not (len(splits) == 1 and sole is splits[0]):
            raise ValueError(
                "Columns layout='split' is the page: two panes, a list and what it opened. "
                "It cannot sit beside another block or inside one."
            )
        return self

    def iter_parts(self, *kinds: type[Part]) -> Iterator[Part]:
        """Every block, value, and control of ``kinds`` on the page, however deep,
        in the order the page holds them."""

        def walk(part: Schema) -> Iterator[Part]:
            for _, value in part:
                for child in value if isinstance(value, list) else [value]:
                    if isinstance(child, Schema):
                        if isinstance(child, kinds):
                            yield child
                        yield from walk(child)

        return walk(self)
