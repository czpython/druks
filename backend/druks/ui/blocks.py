from collections.abc import Iterable
from typing import Annotated, Any, Literal

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

from druks.schemas import Schema

from .fields import Field as FormField


def _subject_identity(value):
    """``follows=`` takes the subject a page or a region watches, or a subject
    class for every subject of that type. Druks streams what it names and
    rereads the page on every snapshot it sends."""
    if isinstance(value, dict | Follows) or value is None:
        return value
    if isinstance(value, type):
        subject_type = getattr(value, "subject_type", "")
        if subject_type:
            return {"subject_type": subject_type, "subject_id": ""}
    else:
        identity = getattr(value, "identity", None)
        if identity:
            # A subject id reaches the stream through a URL, so it travels as text.
            return {"subject_type": identity["type"], "subject_id": str(identity["id"])}
    raise ValueError(
        "follows= takes the subject a page watches, or its class for every subject of that "
        f"type, not {type(value).__name__}. A run is about a subject, and the subject is what "
        "the stream carries."
    )


class Follows(Schema):
    """The subject a page or a named region watches. An empty ``subject_id``
    watches every subject of the type."""

    subject_type: str
    subject_id: str = ""


Watched = Annotated[Follows | None, BeforeValidator(_subject_identity)]


def _check_field_names(*, owner: str, fields: list[FormField], arguments: dict[str, Any]) -> None:
    names = [field.name for field in fields]
    repeated = sorted({name for name in names if names.count(name) > 1})
    if repeated:
        raise ValueError(
            f"{owner} has two fields named {repeated}; one would take the other's value. "
            "Give each field its own name."
        )
    taken = sorted(set(names) & set(arguments))
    if taken:
        raise ValueError(
            f"{owner} has fields named {taken}, which it already carries as arguments. "
            "Send each value once."
        )


class PageBlock(Schema):
    """What every block shares. ``block`` names its kind on the wire, and
    ``check_placement`` is how a block refuses a spot it cannot work in."""

    block: str

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        """Raise when this block cannot sit where the page put it. ``followed``
        says whether any ancestor watches a subject, ``region`` names the nearest
        one around it, and ``regions`` collects the region names already taken."""

    def iter_actions(self) -> "Iterable[Action]":
        """Every action this block offers, however deep."""
        return ()


class BlockParent(PageBlock):
    """A block that holds other blocks."""

    blocks: list["Block"] = Field(default_factory=list)

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        for block in self.blocks:
            block.check_placement(followed=followed, regions=regions, region=region)

    def iter_actions(self) -> "Iterable[Action]":
        for block in self.blocks:
            yield from block.iter_actions()


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
        if self.subject and not self.subject.subject_id:
            raise ValueError(
                f"Link {self.label!r} names the {self.subject.subject_type} type, and a link "
                "opens one subject's page. Give the subject itself."
            )
        if [bool(self.page), bool(self.url), bool(self.subject)].count(True) == 1:
            return self
        raise ValueError(f"Link {self.label!r} must set exactly one of page, url, or subject")


class Action(PageBlock):
    """A control that calls one of the app's own operations. ``operation`` is a
    route's ``operation_id``; the shell resolves it to a method and a URL, so
    the author writes no URL."""

    block: Literal["action"] = "action"
    label: str
    operation: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    fields: list[FormField] = Field(default_factory=list)
    tone: Literal["default", "primary", "danger"] = "default"
    # Non-empty text asks the operator before the shell sends anything.
    confirm: str = ""
    # What happens once the operation answers. A ``link`` navigates, and then
    # ``refresh`` does not apply.
    refresh: Literal["none", "page", "region"] = "page"
    link: Link | None = None

    @model_validator(mode="after")
    def _one_shape(self) -> "Action":
        if self.fields and self.confirm:
            raise ValueError(
                f"Action {self.label!r} gives both fields and confirm. Each asks the operator "
                "before the action runs, so give one or the other."
            )
        return self

    @model_validator(mode="after")
    def _one_name_for_each_value(self) -> "Action":
        _check_field_names(
            owner=f"action {self.label!r}",
            fields=self.fields,
            arguments=self.arguments,
        )
        return self

    def iter_actions(self) -> "Iterable[Action]":
        yield self

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        if self.refresh != "region" or region:
            return
        raise ValueError(
            f"action {self.label!r} refreshes its region, and it sits in none. Put it in a "
            'named Section, or refresh the page with refresh="page".'
        )

    def check_operation(self, app_name: str, operations) -> None:
        """The operation must be one of the app's own writes."""
        found = operations.get(self.operation)
        if not found:
            raise ValueError(
                f"app {app_name!r} action {self.label!r} names operation "
                f"{self.operation!r}, which none of its routes declares. This app declares "
                f"{sorted(operations)}."
            )
        if found.method == "GET":
            raise ValueError(
                f"app {app_name!r} action {self.label!r} names {self.operation!r}, a GET "
                "route. A GET is a read, so it can never be an action."
            )


class Form(PageBlock):
    """Inputs and the action that submits them. The shell sends the action's
    arguments and the field values as one object."""

    block: Literal["form"] = "form"
    title: str = ""
    description: str = ""
    fields: list[FormField] = Field(default_factory=list)
    action: Action

    def iter_actions(self) -> "Iterable[Action]":
        yield self.action

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        self.action.check_placement(followed=followed, regions=regions, region=region)

    @model_validator(mode="after")
    def _one_name_for_each_value(self) -> "Form":
        if self.action.fields:
            raise ValueError(
                f"form {self.title!r} has fields on its action. Put all form fields on the form."
            )
        _check_field_names(
            owner=f"form {self.title!r}",
            fields=self.fields,
            arguments=self.action.arguments,
        )
        return self


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


class Quote(PageBlock):
    """Someone else's words, kept as they arrived — a message, a reply, an
    answer. Line breaks survive; nothing is read as markup."""

    block: Literal["quote"] = "quote"
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
    controls: list[Action | Link] = Field(default_factory=list)

    def iter_actions(self) -> "Iterable[Action]":
        for control in self.controls:
            yield from control.iter_actions()

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        for control in self.controls:
            control.check_placement(followed=followed, regions=regions, region=region)

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

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        if followed:
            return
        raise ValueError(
            f"GateControls for run {self.run!r} sits in nothing that follows a subject, so an "
            "answered gate would stay on screen. Put it in a Page or Section with follows=."
        )


class TextValue(Schema):
    """Words. ``link`` is how a table cell, a fact, or a list item reaches
    another page."""

    def __init__(self, text, **data):
        super().__init__(text=text, **data)

    value: Literal["text"] = "text"
    text: str
    description: str = ""
    link: Link | None = None


class NumberValue(Schema):
    value: Literal["number"] = "number"
    number: float = Field(allow_inf_nan=False)
    unit: str = ""
    tone: Literal["neutral", "active", "success", "warning", "danger"] = "neutral"

    def __init__(self, number, **data):
        super().__init__(number=number, **data)


class StatusValue(Schema):
    """Where something stands. The app writes the word; the tone selects the
    presentation."""

    value: Literal["status"] = "status"
    label: str
    tone: Literal["neutral", "active", "success", "warning", "danger"] = "neutral"

    def __init__(self, label: str, **data):
        super().__init__(label=label, **data)


class TimeValue(Schema):
    value: Literal["time"] = "time"
    when: AwareDatetime

    def __init__(self, when, **data):
        super().__init__(when=when, **data)


Value = Annotated[TextValue | NumberValue | StatusValue | TimeValue, Discriminator("value")]


class TimelineItem(Schema):
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


class ProgressStep(Schema):
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


class FileSummary(Schema):
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


class ChartSeries(Schema):
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


class Metric(Schema):
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


class Fact(Schema):
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


class TableColumn(Schema):
    label: str
    align: Literal["start", "end"] = "start"

    def __init__(self, label, **data):
        super().__init__(label=label, **data)


class TableRow(Schema):
    cells: list[Value] = Field(default_factory=list)
    detail: str = ""

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
    controls: list[Action | Link] = Field(default_factory=list)

    def iter_actions(self) -> "Iterable[Action]":
        yield from super().iter_actions()
        for control in self.controls:
            yield from control.iter_actions()

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        super().check_placement(followed=followed, regions=regions, region=region)
        for control in self.controls:
            control.check_placement(followed=followed, regions=regions, region=region)


class Cards(PageBlock):
    """One card for each of a set of things. The shell arranges them, so a page
    that wants a particular geometry reaches for ``Columns`` instead."""

    block: Literal["cards"] = "cards"
    title: str = ""
    cards: list[Card] = Field(default_factory=list)
    empty: EmptyState | None = None

    def iter_actions(self) -> "Iterable[Action]":
        for card in self.cards:
            yield from card.iter_actions()
        if self.empty:
            yield from self.empty.iter_actions()

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        for card in self.cards:
            card.check_placement(followed=followed, regions=regions, region=region)
        if self.empty:
            self.empty.check_placement(followed=followed, regions=regions, region=region)


class Section(BlockParent):
    """A titled part of a page. A ``name`` makes it a region the shell can
    replace on its own, and ``follows`` is what makes it do so."""

    block: Literal["section"] = "section"
    title: str = ""
    name: str = ""
    controls: list[Action | Link] = Field(default_factory=list)
    follows: Watched = None

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        if self.name in regions:
            raise ValueError(
                f"this page has two regions named {self.name!r}; the shell replaces a region "
                "by name, so it could not tell them apart. Give each one its own name."
            )
        if self.name:
            regions.add(self.name)
        inside = self.name or region
        for control in self.controls:
            control.check_placement(
                followed=followed or bool(self.follows),
                regions=regions,
                region=inside,
            )
        super().check_placement(
            followed=followed or bool(self.follows),
            regions=regions,
            region=inside,
        )

    def iter_actions(self) -> "Iterable[Action]":
        for control in self.controls:
            yield from control.iter_actions()
        yield from super().iter_actions()

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
    | Quote
    | Section
    | Card
    | Cards
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
    | Columns
    | Action
    | Form,
    Field(discriminator="block"),
]

Card.model_rebuild()
Cards.model_rebuild()
Section.model_rebuild()
Stack.model_rebuild()
Columns.model_rebuild()
BlockParent.model_rebuild()
