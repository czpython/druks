from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, model_validator

from druks.schemas import BaseResponse


def _subject_identity(value):
    """``follows=`` takes the subject a page or a region watches. Druks streams
    that subject and rereads the page on every snapshot it sends."""
    if isinstance(value, dict | Follows) or value is None:
        return value
    identity = getattr(value, "identity", None)
    if not identity:
        raise ValueError(
            f"follows= takes the subject a page watches, not {type(value).__name__}. A run is "
            "about a subject, and the subject is what the stream carries."
        )
    # A subject id reaches the stream through a URL, so it travels as text.
    return {"subject_type": identity["type"], "subject_id": str(identity["id"])}


class Follows(BaseResponse):
    """The subject a page or a named region watches."""

    subject_type: str
    subject_id: str


Watched = Annotated[Follows | None, BeforeValidator(_subject_identity)]


class PageBlock(BaseResponse):
    """What every block shares. ``block`` names its kind on the wire, and
    ``check_placement`` is how a block refuses a spot it cannot work in."""

    block: str

    def check_placement(self, *, followed: bool, regions: set[str]) -> None:
        """Raise when this block cannot sit where the page put it. ``followed``
        says whether any ancestor watches a subject; ``regions`` collects the
        region names already taken."""


class BlockParent(PageBlock):
    """A block that holds other blocks."""

    blocks: list["Block"] = Field(default_factory=list)

    def check_placement(self, *, followed: bool, regions: set[str]) -> None:
        for block in self.blocks:
            block.check_placement(followed=followed, regions=regions)


class Link(PageBlock):
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


class Text(PageBlock):
    block: Literal["text"] = "text"
    text: str

    def __init__(self, text: str, **data):
        super().__init__(text=text, **data)


class Markdown(PageBlock):
    block: Literal["markdown"] = "markdown"
    text: str

    def __init__(self, text: str, **data):
        super().__init__(text=text, **data)


class Callout(PageBlock):
    """A short message the reader should not miss. The tone selects the
    presentation; the app writes the words."""

    block: Literal["callout"] = "callout"
    tone: Literal["info", "success", "warning", "danger"] = "info"
    title: str = ""
    text: str

    def __init__(self, text: str, **data):
        super().__init__(text=text, **data)


class Divider(PageBlock):
    block: Literal["divider"] = "divider"


class EmptyState(PageBlock):
    """What a page shows in place of content it has none of."""

    block: Literal["empty_state"] = "empty_state"
    title: str
    description: str = ""
    actions: list[Link] = Field(default_factory=list)

    def __init__(self, title: str, **data):
        super().__init__(title=title, **data)


class GateControls(PageBlock):
    """The operator's answer to a parked run, derived from the run itself. The
    shell reads the ask, the options, and the artifact from the gate, and
    submits the answer with the run's ``parkedAt``."""

    block: Literal["gate_controls"] = "gate_controls"
    run: str

    def __init__(self, run: str, **data):
        super().__init__(run=run, **data)

    def check_placement(self, *, followed: bool, regions: set[str]) -> None:
        if followed:
            return
        raise ValueError(
            f"GateControls for run {self.run!r} sits in nothing that follows a subject, so an "
            "answered gate would stay on screen. Put it in a Page or Section with follows=."
        )


class Card(BlockParent):
    block: Literal["card"] = "card"
    title: str = ""
    description: str = ""
    actions: list[Link] = Field(default_factory=list)


class Section(BlockParent):
    """A titled part of a page. A ``name`` makes it a region the shell can
    replace on its own, and ``follows`` is what makes it do so."""

    block: Literal["section"] = "section"
    title: str = ""
    name: str = ""
    follows: Watched = None

    def check_placement(self, *, followed: bool, regions: set[str]) -> None:
        if self.name in regions:
            raise ValueError(
                f"this page has two regions named {self.name!r}; the shell replaces a region "
                "by name, so it could not tell them apart. Give each one its own name."
            )
        if self.name:
            regions.add(self.name)
        super().check_placement(followed=followed or bool(self.follows), regions=regions)

    @model_validator(mode="after")
    def _named_when_followed(self) -> "Section":
        if self.follows and not self.name:
            raise ValueError(
                "a Section that follows a subject needs a name — the shell replaces it by name"
            )
        return self


Block = Annotated[
    Text | Markdown | Section | Card | Callout | Divider | EmptyState | Link | GateControls,
    Field(discriminator="block"),
]

Card.model_rebuild()
Section.model_rebuild()
BlockParent.model_rebuild()
