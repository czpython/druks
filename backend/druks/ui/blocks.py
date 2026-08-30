from typing import Annotated, Literal

from pydantic import Field, model_validator

from druks.schemas import BaseResponse


class Link(BaseResponse):
    """A control that navigates: to another page of this app, or outside."""

    block: Literal["link"] = "link"
    label: str
    page: str = ""
    arguments: dict[str, str] = Field(default_factory=dict)
    url: str = ""

    def __init__(self, label: str, **data):
        super().__init__(label=label, **data)

    @model_validator(mode="after")
    def _one_destination(self) -> "Link":
        if bool(self.page) != bool(self.url):
            return self
        raise ValueError(f"Link {self.label!r} must set page or url, never both and never neither")


class Text(BaseResponse):
    block: Literal["text"] = "text"
    text: str

    def __init__(self, text: str, **data):
        super().__init__(text=text, **data)


class Markdown(BaseResponse):
    block: Literal["markdown"] = "markdown"
    text: str

    def __init__(self, text: str, **data):
        super().__init__(text=text, **data)


class Callout(BaseResponse):
    """A short message the reader should not miss. The tone selects the
    presentation; the app writes the words."""

    block: Literal["callout"] = "callout"
    tone: Literal["info", "success", "warning", "danger"] = "info"
    title: str = ""
    text: str

    def __init__(self, text: str, **data):
        super().__init__(text=text, **data)


class Divider(BaseResponse):
    block: Literal["divider"] = "divider"


class EmptyState(BaseResponse):
    """What a page shows in place of content it has none of."""

    block: Literal["empty_state"] = "empty_state"
    title: str
    description: str = ""
    actions: list[Link] = Field(default_factory=list)

    def __init__(self, title: str, **data):
        super().__init__(title=title, **data)


class Card(BaseResponse):
    block: Literal["card"] = "card"
    title: str = ""
    description: str = ""
    blocks: list["Block"] = Field(default_factory=list)
    actions: list[Link] = Field(default_factory=list)


class Section(BaseResponse):
    """A titled part of a page. A ``name`` makes it a region the shell can
    replace on its own."""

    block: Literal["section"] = "section"
    title: str = ""
    name: str = ""
    blocks: list["Block"] = Field(default_factory=list)


Block = Annotated[
    Text | Markdown | Section | Card | Callout | Divider | EmptyState | Link,
    Field(discriminator="block"),
]

Card.model_rebuild()
Section.model_rebuild()
