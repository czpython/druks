from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BeforeValidator,
    ConfigDict,
    Discriminator,
    Field,
    StringConstraints,
    computed_field,
    model_validator,
)

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
    """A control that navigates: to another page of this app, to the subject's
    own platform page, or outside."""

    block: Literal["link"] = "link"
    label: str
    page: str = ""
    arguments: dict[str, str] = Field(default_factory=dict)
    url: str = ""
    subject: Watched = None

    def __init__(self, label: str, **data):
        super().__init__(label=label, **data)

    @model_validator(mode="after")
    def _one_destination(self) -> "Link":
        if [bool(self.page), bool(self.url), bool(self.subject)].count(True) == 1:
            return self
        raise ValueError(f"Link {self.label!r} must set exactly one of page, url, or subject")


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


class TextValue(BaseResponse):
    """Words. ``link`` is how a table cell, a fact, or a list item reaches
    another page."""

    def __init__(self, text, **data):
        super().__init__(text=text, **data)

    value: Literal["text"] = "text"
    text: str
    link: Link | None = None


class NumberValue(BaseResponse):
    value: Literal["number"] = "number"
    number: float = Field(allow_inf_nan=False)
    unit: str = ""

    def __init__(self, number, **data):
        super().__init__(number=number, **data)


class StatusValue(BaseResponse):
    """Where something stands. The app writes the word; the tone selects the
    presentation."""

    value: Literal["status"] = "status"
    label: str
    tone: Literal["neutral", "active", "success", "warning", "danger"] = "neutral"

    def __init__(self, label: str, **data):
        super().__init__(label=label, **data)


class TimeValue(BaseResponse):
    value: Literal["time"] = "time"
    when: AwareDatetime

    def __init__(self, when, **data):
        super().__init__(when=when, **data)


Value = Annotated[TextValue | NumberValue | StatusValue | TimeValue, Discriminator("value")]


class TimelineItem(BaseResponse):
    # Aware, so items from different sources order against each other.
    when: AwareDatetime
    title: str
    description: str = ""
    status: StatusValue | None = None


class Timeline(PageBlock):
    """What happened, in order."""

    block: Literal["timeline"] = "timeline"
    title: str = ""
    items: list[TimelineItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def _oldest_first(self) -> "Timeline":
        # Druks orders here, where the stamps keep their full precision: a
        # browser compares milliseconds and would call two microseconds apart
        # the same moment. Sorting is stable, so items that truly share a
        # moment keep the order the app gave them.
        self.items.sort(key=lambda item: item.when)
        return self

    def __init__(self, items=(), **data):
        super().__init__(items=items, **data)


class ProgressStep(BaseResponse):
    label: str
    status: StatusValue


class Progress(PageBlock):
    """How far along work is. Set ``completed`` for a share of ``total``, give
    ``steps`` for staged work, or give neither when the end is unknown."""

    block: Literal["progress"] = "progress"
    label: str
    completed: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    total: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    steps: list[ProgressStep] = Field(default_factory=list)

    def __init__(self, label: str, **data):
        super().__init__(label=label, **data)

    @model_validator(mode="after")
    def _one_shape(self) -> "Progress":
        if self.completed is None:
            return self
        if self.steps:
            raise ValueError(
                f"Progress {self.label!r} gives both completed and steps. Staged work reads as "
                "its steps, so give one or the other."
            )
        if self.completed > self.total:
            raise ValueError(
                f"Progress {self.label!r} has completed {self.completed} of {self.total}."
            )
        return self


class Image(PageBlock):
    """One image. ``alternative_text`` is what a reader gets in place of it, so
    it says what the image shows, not that it is an image."""

    block: Literal["image"] = "image"
    url: str
    alternative_text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    caption: str = ""


class FileSummary(BaseResponse):
    """One file, as the shell shows it. The download is derived from the id, so
    a file always travels through the platform's own route and its identity
    gate."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    content_type: str
    size: int

    @computed_field
    @property
    def url(self) -> str:
        return f"/api/files/{self.id}"


class Files(PageBlock):
    """Files an operator can preview or download. ``files`` takes
    ``druks.files.File`` objects."""

    block: Literal["files"] = "files"
    title: str = ""
    files: list[FileSummary] = Field(default_factory=list)

    def __init__(self, files=(), **data):
        super().__init__(files=files, **data)


class ChartSeries(BaseResponse):
    label: str
    points: list[Annotated[float, Field(allow_inf_nan=False)]]


class Chart(PageBlock):
    """Numbers over categories. Every series carries one point for each
    category, so the shell can read the same table a chart draws."""

    block: Literal["chart"] = "chart"
    kind: Literal["line", "bar", "area"] = "line"
    title: str = ""
    categories: list[str] = Field(default_factory=list)
    series: list[ChartSeries] = Field(default_factory=list)
    category_label: str = ""
    value_label: str = ""

    @model_validator(mode="after")
    def _series_match_the_categories(self) -> "Chart":
        wrong = [one.label for one in self.series if len(one.points) != len(self.categories)]
        if wrong:
            raise ValueError(
                f"chart series {wrong} do not carry one point for each of the "
                f"{len(self.categories)} categories."
            )
        return self


class ImageGallery(PageBlock):
    block: Literal["image_gallery"] = "image_gallery"
    title: str = ""
    images: list[Image] = Field(default_factory=list)

    def __init__(self, images=(), **data):
        super().__init__(images=images, **data)


class Metric(BaseResponse):
    label: str
    value: Value
    description: str = ""

    def __init__(self, label, **data):
        super().__init__(label=label, **data)


class Metrics(PageBlock):
    block: Literal["metrics"] = "metrics"
    title: str = ""
    metrics: list[Metric] = Field(default_factory=list)

    def __init__(self, metrics=(), **data):
        super().__init__(metrics=metrics, **data)


class Fact(BaseResponse):
    label: str
    value: Value

    def __init__(self, label, **data):
        super().__init__(label=label, **data)


class Facts(PageBlock):
    """The label-and-value list."""

    block: Literal["facts"] = "facts"
    title: str = ""
    facts: list[Fact] = Field(default_factory=list)

    def __init__(self, facts=(), **data):
        super().__init__(facts=facts, **data)


class TableColumn(BaseResponse):
    label: str
    align: Literal["start", "end"] = "start"

    def __init__(self, label, **data):
        super().__init__(label=label, **data)


class TableRow(BaseResponse):
    cells: list[Value] = Field(default_factory=list)

    def __init__(self, cells=(), **data):
        super().__init__(cells=cells, **data)


class Table(PageBlock):
    """Rows of values under named columns. Every row carries one cell for each
    column; with no rows the shell shows ``empty_text``."""

    block: Literal["table"] = "table"
    title: str = ""
    columns: list[TableColumn] = Field(default_factory=list)
    rows: list[TableRow] = Field(default_factory=list)
    empty_text: str = ""

    @model_validator(mode="after")
    def _rows_match_the_columns(self) -> "Table":
        wrong = [len(row.cells) for row in self.rows if len(row.cells) != len(self.columns)]
        if wrong:
            raise ValueError(
                f"table {self.title!r} has rows of {sorted(set(wrong))} cells under "
                f"{len(self.columns)} columns."
            )
        return self


class List(PageBlock):
    block: Literal["list"] = "list"
    title: str = ""
    items: list[Value] = Field(default_factory=list)

    def __init__(self, items=(), **data):
        super().__init__(items=items, **data)


class Stack(BlockParent):
    """Blocks down the page."""

    block: Literal["stack"] = "stack"
    gap: Literal["small", "medium", "large"] = "medium"

    def __init__(self, blocks=(), **data):
        super().__init__(blocks=blocks, **data)


class Columns(BlockParent):
    """Blocks across the page. Each child is one column; they share the width
    and stack on a narrow screen."""

    block: Literal["columns"] = "columns"

    def __init__(self, blocks=(), **data):
        super().__init__(blocks=blocks, **data)


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
    Text
    | Markdown
    | Section
    | Card
    | Callout
    | Divider
    | EmptyState
    | Link
    | GateControls
    | Timeline
    | Progress
    | Image
    | Files
    | Chart
    | ImageGallery
    | Metrics
    | Facts
    | Table
    | List
    | Stack
    | Columns,
    Field(discriminator="block"),
]

Card.model_rebuild()
Section.model_rebuild()
Stack.model_rebuild()
Columns.model_rebuild()
BlockParent.model_rebuild()
