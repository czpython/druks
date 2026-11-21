import re
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypeVar
from urllib.parse import quote

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BeforeValidator,
    ConfigDict,
    Discriminator,
    Field,
    SerializationInfo,
    SerializerFunctionWrapHandler,
    StringConstraints,
    TypeAdapter,
    computed_field,
    model_serializer,
    model_validator,
)

from druks.schemas import Schema

from .exceptions import NoTextFormError
from .fields import Field as FormField

if TYPE_CHECKING:
    from .page import PageRoute

Part = TypeVar("Part", bound=Schema)

# An address that names its own host, and a path from the root of the dashboard.
_ABSOLUTE_URL = re.compile(r"https?://", re.IGNORECASE)
_DASHBOARD_PATH = re.compile(r"/(?!/)")

# The rule under the header of a table, for the alignment of each column.
_ALIGNMENT_RULE = {"start": "---", "end": "---:"}


def _subject_identity(value):
    """``follows=`` takes the subject a page or a region watches, or a subject
    class for every subject of that type. Druks streams what it names and
    rereads the page on every snapshot it sends."""
    if not value or isinstance(value, dict | Follows):
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


def _one_subject(follows: Follows) -> Follows:
    if follows.subject_id:
        return follows
    raise ValueError(
        f"this shows the work on one {follows.subject_type}, not on every one. "
        "Give the subject itself."
    )


Subject = Annotated[Follows, BeforeValidator(_subject_identity), AfterValidator(_one_subject)]


def _get_status(subject: Follows, info: SerializationInfo) -> dict[str, Any]:
    # A read of a page or of an ask hands in where the work on each subject stands.
    status = info.context["statuses"][(subject.subject_type, subject.subject_id)]
    return status.model_dump(mode=info.mode, by_alias=info.by_alias)


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


def iter_parts(parts: Iterable[Schema], *kinds: type[Part]) -> Iterator[Part]:
    """Every block, value, and control of ``kinds`` among ``parts`` and inside
    them, however deep, in the order they hold them."""
    for part in parts:
        if isinstance(part, kinds):
            yield part
        for _, value in part:
            children = [value]
            if isinstance(value, list):
                children = value
            yield from iter_parts(
                [child for child in children if isinstance(child, Schema)], *kinds
            )


def _join(parts: Iterable[str]) -> str:
    return "\n\n".join(part for part in parts if part)


def _heading(title: str) -> str:
    if title:
        return f"### {title}"
    return ""


def _titled(title: str, body: str) -> str:
    # An empty block writes nothing, not even its title, as the dashboard shows nothing.
    if body:
        return _join([_heading(title), body])
    return ""


def _bullets(items: Iterable[str]) -> str:
    # The lines of an item keep its indent, so an item of two paragraphs stays one item.
    return "\n".join("- " + item.replace("\n", "\n  ") for item in items)


def _quote(text: str) -> str:
    return "\n".join(f"> {line}".rstrip() for line in text.split("\n"))


def _number(number: float) -> str:
    # Grouped, in full, and never in exponent form, as the dashboard shows it.
    return f"{Decimal(str(number)).normalize():,f}"


def _time(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _table(header: list[str], rows: Iterable[list[str]], aligns: Sequence[str] = ()) -> str:
    rule = [_ALIGNMENT_RULE[align] for align in aligns] or ["---"] * len(header)
    # A pipe would end a cell and a line break its row, so a cell keeps neither.
    return "\n".join(
        "| " + " | ".join(" ".join(cell.replace("|", "\\|").splitlines()) for cell in line) + " |"
        for line in [header, rule, *rows]
    )


def _link(words: str, path: str, endpoint: str) -> str:
    if _ABSOLUTE_URL.match(path):
        return f"[{words}]({path})"
    if _DASHBOARD_PATH.match(path) and endpoint:
        return f"[{words}]({endpoint}{path})"
    return words


def _controls(
    controls: "Sequence[Action | Link]",
    *,
    app: str,
    pages: Sequence["PageRoute"],
    endpoint: str,
) -> str:
    return " · ".join(
        control.to_markdown(app=app, pages=pages, endpoint=endpoint) for control in controls
    )


def _linked(
    words: str, link: "Link | None", *, app: str, pages: Sequence["PageRoute"], endpoint: str
) -> str:
    # A link on a value or a card takes the words it sits on.
    if link and words:
        return link.model_copy(update={"label": words}).to_markdown(
            app=app, pages=pages, endpoint=endpoint
        )
    return words


class PageBlock(Schema):
    """What every block shares. ``block`` names its kind on the wire,
    ``check_placement`` is how a block refuses a spot it cannot work in, and
    ``to_markdown`` is how it reads as text."""

    block: str

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        """Raise when this block cannot sit where the page put it. ``followed``
        says whether any ancestor watches a subject, ``region`` names the nearest
        one around it, and ``regions`` collects the region names already taken."""

    def to_markdown(
        self, *, app: str = "", pages: Sequence["PageRoute"] = (), endpoint: str = ""
    ) -> str:
        """The block as markdown that names no channel. A link to a page or a
        subject opens on the dashboard at ``endpoint``, on the page that ``app``
        declares in ``pages``. A link with no address reads as its words."""
        raise NotImplementedError(f"{type(self).__name__} has no to_markdown()")


class BlockParent(PageBlock):
    """A block that holds other blocks."""

    blocks: list["Block"] = Field(default_factory=list)

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        for block in self.blocks:
            block.check_placement(followed=followed, regions=regions, region=region)

    def to_markdown(
        self, *, app: str = "", pages: Sequence["PageRoute"] = (), endpoint: str = ""
    ) -> str:
        return _join(
            block.to_markdown(app=app, pages=pages, endpoint=endpoint) for block in self.blocks
        )


class Link(PageBlock):
    """A control that navigates: to another page of this app, to the subject's
    own platform page, or outside. A link on a value takes the value's words,
    so it needs no label of its own."""

    # A route argument travels in a URL, so an id reads as its text.
    model_config = ConfigDict(coerce_numbers_to_str=True)

    block: Literal["link"] = "link"
    label: str = ""
    page: str = ""
    arguments: dict[str, str] = Field(default_factory=dict)
    url: str = ""
    subject: Watched = None

    def __init__(self, label: str = "", **data):
        super().__init__(label=label, **data)

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        if self.label:
            return
        raise ValueError("a Link that stands on its own has no label. Give it the words it shows.")

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

    def to_markdown(
        self, *, app: str = "", pages: Sequence["PageRoute"] = (), endpoint: str = ""
    ) -> str:
        path = self.url
        if self.subject:
            subject_type, subject_id = self.subject.subject_type, self.subject.subject_id
            path = f"/{app}/{quote(subject_type, safe='')}/{quote(subject_id, safe='')}"
            for page in pages:
                if page.subject and page.subject.subject_type == subject_type:
                    path = f"/{app}{page.get_path(dict.fromkeys(page.parameters, subject_id))}"
        elif self.page:
            for page in pages:
                # A link that lacks an argument of its page's route has no address.
                if page.name == self.page and set(page.parameters) <= set(self.arguments):
                    path = f"/{app}{page.get_path(self.arguments)}".rstrip("/")
        return _link(self.label, path, endpoint)


class Interactive(PageBlock):
    """A block that sends what the operator does: a press, a form, an answer.
    Text cannot send, so it has no text form."""

    def to_markdown(
        self, *, app: str = "", pages: Sequence["PageRoute"] = (), endpoint: str = ""
    ) -> str:
        raise NoTextFormError(type(self).__name__)


class Action(Interactive):
    """A control that calls one of the app's own operations. ``operation`` is a
    route's ``operation_id``; the shell resolves it to a method and a URL, so
    the author writes no URL.

    After a send, the shell navigates when the action sets ``link``, or when
    the operation answers exactly ``{"url": ...}`` with an absolute http(s)
    URL. ``refresh`` applies only when neither navigates."""

    block: Literal["action"] = "action"
    label: str
    operation: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    fields: list[FormField] = Field(default_factory=list)
    tone: Literal["default", "primary", "danger"] = "default"
    # Non-empty text asks the operator before the shell sends anything.
    confirm: str = ""
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


class Form(Interactive):
    """Inputs and the action that submits them. The shell sends the action's
    arguments and the field values as one object. ``extra_actions`` are more
    buttons that send the same fields. ``submit="change"`` sends on blur for
    text and on change for a select, with no button."""

    block: Literal["form"] = "form"
    title: str = ""
    description: str = ""
    fields: list[FormField] = Field(default_factory=list)
    action: Action
    extra_actions: list[Action] = Field(default_factory=list)
    submit: Literal["button", "change"] = "button"
    layout: Literal["stack", "prose", "row"] = "stack"

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        self.action.check_placement(followed=followed, regions=regions, region=region)
        for extra in self.extra_actions:
            extra.check_placement(followed=followed, regions=regions, region=region)

    @model_validator(mode="after")
    def _one_name_for_each_value(self) -> "Form":
        if self.action.fields or any(extra.fields for extra in self.extra_actions):
            raise ValueError(
                f"form {self.title!r} has fields on its action. Put all form fields on the form."
            )
        if self.submit == "change" and self.extra_actions:
            raise ValueError(
                f"form {self.title!r} submits on change and also has extra actions. "
                "An extra action is a button; give the form a button, or drop extra_actions."
            )
        if self.submit == "change" and self.action.confirm:
            raise ValueError(
                f"form {self.title!r} submits on change and also asks to confirm. "
                "A confirm is a press; give the form a button, or drop confirm."
            )
        _check_field_names(
            owner=f"form {self.title!r}",
            fields=self.fields,
            arguments=self.action.arguments,
        )
        for extra in self.extra_actions:
            _check_field_names(
                owner=f"form {self.title!r} action {extra.label!r}",
                fields=self.fields,
                arguments=extra.arguments,
            )
        return self


class Text(PageBlock):
    block: Literal["text"] = "text"
    text: str

    def __init__(self, text: str, **data):
        super().__init__(text=text, **data)

    def to_markdown(
        self, *, app: str = "", pages: Sequence["PageRoute"] = (), endpoint: str = ""
    ) -> str:
        return self.text


class Markdown(PageBlock):
    block: Literal["markdown"] = "markdown"
    text: str

    def __init__(self, text: str, **data):
        super().__init__(text=text, **data)

    def to_markdown(
        self, *, app: str = "", pages: Sequence["PageRoute"] = (), endpoint: str = ""
    ) -> str:
        return self.text


class Quote(PageBlock):
    """Someone else's words, kept as they arrived — a message, a reply, an
    answer. Line breaks survive; nothing is read as markup."""

    block: Literal["quote"] = "quote"
    text: str

    def __init__(self, text: str, **data):
        super().__init__(text=text, **data)

    def to_markdown(
        self, *, app: str = "", pages: Sequence["PageRoute"] = (), endpoint: str = ""
    ) -> str:
        return _quote(self.text)


class Callout(PageBlock):
    """A short message the reader should not miss. The tone selects the
    presentation; the app writes the words. ``controls`` are the next step
    the message points at."""

    block: Literal["callout"] = "callout"
    tone: Literal["info", "success", "warning", "danger"] = "info"
    title: str = ""
    text: str
    controls: list[Action | Link] = Field(default_factory=list)

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        for control in self.controls:
            control.check_placement(followed=followed, regions=regions, region=region)

    def __init__(self, text: str, **data):
        super().__init__(text=text, **data)

    def to_markdown(
        self, *, app: str = "", pages: Sequence["PageRoute"] = (), endpoint: str = ""
    ) -> str:
        title = ""
        if self.title:
            title = f"**{self.title}**"
        return _quote(
            _join(
                [
                    title,
                    self.text,
                    _controls(self.controls, app=app, pages=pages, endpoint=endpoint),
                ]
            )
        )


class Divider(PageBlock):
    block: Literal["divider"] = "divider"

    def to_markdown(
        self, *, app: str = "", pages: Sequence["PageRoute"] = (), endpoint: str = ""
    ) -> str:
        return "---"


class EmptyState(PageBlock):
    """What a page shows in place of content it has none of."""

    block: Literal["empty_state"] = "empty_state"
    title: str
    description: str = ""
    controls: list[Action | Link] = Field(default_factory=list)

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        for control in self.controls:
            control.check_placement(followed=followed, regions=regions, region=region)

    def __init__(self, title: str, **data):
        super().__init__(title=title, **data)

    def to_markdown(
        self, *, app: str = "", pages: Sequence["PageRoute"] = (), endpoint: str = ""
    ) -> str:
        return _join(
            [
                self.title,
                self.description,
                _controls(self.controls, app=app, pages=pages, endpoint=endpoint),
            ]
        )


class PageValue(Schema):
    """What every value shares."""

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        """Raise when this value cannot sit where the page put it."""

    def to_markdown(
        self, *, app: str = "", pages: Sequence["PageRoute"] = (), endpoint: str = ""
    ) -> str:
        """The value as markdown, the way a fact, a list item, or a table cell
        writes it. The arguments are those of ``PageBlock.to_markdown``."""
        raise NotImplementedError(f"{type(self).__name__} has no to_markdown()")


class TextValue(PageValue):
    """Words. ``link`` is how a table cell, a fact, or a list item reaches
    another page."""

    def __init__(self, text, **data):
        super().__init__(text=text, **data)

    value: Literal["text"] = "text"
    text: str
    description: str = ""
    link: Link | None = None

    def to_markdown(
        self, *, app: str = "", pages: Sequence["PageRoute"] = (), endpoint: str = ""
    ) -> str:
        return _linked(self.text, self.link, app=app, pages=pages, endpoint=endpoint)


class NumberValue(PageValue):
    value: Literal["number"] = "number"
    number: float = Field(allow_inf_nan=False)
    unit: str = ""
    tone: Literal["neutral", "active", "success", "warning", "danger"] = "neutral"

    def __init__(self, number, **data):
        super().__init__(number=number, **data)

    def to_markdown(
        self, *, app: str = "", pages: Sequence["PageRoute"] = (), endpoint: str = ""
    ) -> str:
        if self.unit:
            return f"{_number(self.number)} {self.unit}"
        return _number(self.number)


class StatusValue(PageValue):
    """Where something stands. The app writes the word; the tone selects the
    presentation. ``link`` reaches the thing it names."""

    value: Literal["status"] = "status"
    label: str
    tone: Literal["neutral", "active", "success", "warning", "danger"] = "neutral"
    link: Link | None = None

    def __init__(self, label: str, **data):
        super().__init__(label=label, **data)

    def to_markdown(
        self, *, app: str = "", pages: Sequence["PageRoute"] = (), endpoint: str = ""
    ) -> str:
        return _linked(self.label, self.link, app=app, pages=pages, endpoint=endpoint)


class SubjectStatus(PageValue):
    """Where the work on one subject stands. The shell shows "needs you" when
    the subject waits on the operator, ``working`` while Druks works on it, what
    stopped it, or "idle". Druks reads ``status`` each time it serves the value,
    so a stored value keeps none."""

    value: Literal["subject_status"] = "subject_status"
    subject: Subject
    working: str = "working"
    status: dict | None = Field(default=None, exclude_if=lambda status: not status)

    def __init__(self, subject, **data):
        super().__init__(subject=subject, **data)

    @model_serializer(mode="wrap")
    def _with_status(
        self, serialize: SerializerFunctionWrapHandler, info: SerializationInfo
    ) -> dict[str, Any]:
        if info.context:
            return {**serialize(self), "status": _get_status(self.subject, info)}
        return serialize(self)

    def to_markdown(
        self, *, app: str = "", pages: Sequence["PageRoute"] = (), endpoint: str = ""
    ) -> str:
        # The word that the dashboard writes. A stored value has no status to read.
        if not self.status:
            return ""
        if self.status["gate"]:
            return "needs you"
        if self.status["state"] in ("failed", "cancelled", "orphaned"):
            return self.status["state"]
        if self.status["state"] in ("scheduled", "running"):
            return self.working
        return "idle"


class GateControls(Interactive):
    """The operator's answer to what a subject waits on. The shell reads the
    ask, the options, and the artifact, and shows nothing while the subject
    waits on nothing."""

    block: Literal["gate_controls"] = "gate_controls"
    subject: Subject

    def __init__(self, subject, **data):
        super().__init__(subject=subject, **data)

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        if followed:
            return
        raise ValueError(
            "GateControls sits in nothing that follows a subject, so an answered gate would "
            "stay on screen. Put it in a Page or Section with follows=."
        )

    @model_serializer(mode="wrap")
    def _with_status(
        self, serialize: SerializerFunctionWrapHandler, info: SerializationInfo
    ) -> dict[str, Any]:
        return {**serialize(self), "status": _get_status(self.subject, info)}


class TimeValue(PageValue):
    """A moment. ``empty`` is the word the shell shows when there is none."""

    value: Literal["time"] = "time"
    when: AwareDatetime | None
    empty: str = ""

    def __init__(self, when, **data):
        super().__init__(when=when, **data)

    def to_markdown(
        self, *, app: str = "", pages: Sequence["PageRoute"] = (), endpoint: str = ""
    ) -> str:
        if self.when:
            return _time(self.when)
        return self.empty


class ControlsValue(PageValue):
    """Actions and links in a cell, a fact, or a list item."""

    def __init__(self, controls=(), **data):
        super().__init__(controls=controls, **data)

    value: Literal["controls"] = "controls"
    controls: list[Action | Link] = Field(default_factory=list)

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        for control in self.controls:
            control.check_placement(followed=followed, regions=regions, region=region)

    def to_markdown(
        self, *, app: str = "", pages: Sequence["PageRoute"] = (), endpoint: str = ""
    ) -> str:
        return _controls(self.controls, app=app, pages=pages, endpoint=endpoint)


def _typed_value(value):
    if isinstance(value, str):
        return TextValue(value)
    if isinstance(value, int | float) and not isinstance(value, bool):
        return NumberValue(value)
    if isinstance(value, PageValue | dict):
        return value
    raise ValueError(
        f"this takes a string, a number, or a Value, not {type(value).__name__}. "
        "Give the words to show, or a Value such as a StatusValue or a TimeValue."
    )


Value = Annotated[
    TextValue | NumberValue | StatusValue | SubjectStatus | TimeValue | ControlsValue,
    Discriminator("value"),
    BeforeValidator(_typed_value),
]


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

    def to_markdown(
        self, *, app: str = "", pages: Sequence["PageRoute"] = (), endpoint: str = ""
    ) -> str:
        return _titled(
            self.title, _bullets(f"{_time(item.when)}: {item.title}" for item in self.items)
        )


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

    def to_markdown(
        self, *, app: str = "", pages: Sequence["PageRoute"] = (), endpoint: str = ""
    ) -> str:
        if self.steps:
            steps = (
                (step.label, step.status.to_markdown(app=app, pages=pages, endpoint=endpoint))
                for step in self.steps
            )
            return _join([self.label, _bullets(f"{label}: {status}" for label, status in steps)])
        if self.completed is None:
            return f"{self.label}: still running"
        return f"{self.label}: {_number(self.completed)} of {_number(self.total)}"


class Image(PageBlock):
    """One image. ``alternative_text`` is what a reader gets in place of it, so
    it says what the image shows, not that it is an image."""

    block: Literal["image"] = "image"
    url: str
    alternative_text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    caption: str = ""

    def to_markdown(
        self, *, app: str = "", pages: Sequence["PageRoute"] = (), endpoint: str = ""
    ) -> str:
        return _link(self.alternative_text, self.url, endpoint)


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

    def to_markdown(
        self, *, app: str = "", pages: Sequence["PageRoute"] = (), endpoint: str = ""
    ) -> str:
        return _titled(
            self.title, _bullets(_link(file.name, file.url, endpoint) for file in self.files)
        )


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

    def to_markdown(
        self, *, app: str = "", pages: Sequence["PageRoute"] = (), endpoint: str = ""
    ) -> str:
        # The same table that the dashboard gives a reader who gets no picture.
        title = " — ".join(part for part in [self.title, self.value_label] if part)
        header = [self.category_label or "category", *(one.label for one in self.series)]
        points = zip(self.categories, *(one.points for one in self.series), strict=True)
        rows = [[category, *map(_number, values)] for category, *values in points]
        return _join([_heading(title), _table(header, rows)])


class ImageGallery(PageBlock):
    block: Literal["image_gallery"] = "image_gallery"
    title: str = ""
    images: list[Image] = Field(default_factory=list)

    def __init__(self, images=(), **data):
        super().__init__(images=images, **data)

    def to_markdown(
        self, *, app: str = "", pages: Sequence["PageRoute"] = (), endpoint: str = ""
    ) -> str:
        return _titled(
            self.title,
            _bullets(
                image.to_markdown(app=app, pages=pages, endpoint=endpoint) for image in self.images
            ),
        )


class Metric(Schema):
    label: str
    value: Value
    description: str = ""

    def __init__(self, label, value, **data):
        super().__init__(label=label, value=value, **data)


class Metrics(PageBlock):
    block: Literal["metrics"] = "metrics"
    title: str = ""
    metrics: list[Metric] = Field(default_factory=list)

    def __init__(self, metrics=(), **data):
        super().__init__(metrics=metrics, **data)

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        for metric in self.metrics:
            metric.value.check_placement(followed=followed, regions=regions, region=region)

    def to_markdown(
        self, *, app: str = "", pages: Sequence["PageRoute"] = (), endpoint: str = ""
    ) -> str:
        metrics = (
            (metric.label, metric.value.to_markdown(app=app, pages=pages, endpoint=endpoint))
            for metric in self.metrics
        )
        return _titled(self.title, _bullets(f"**{label}**: {value}" for label, value in metrics))


class Fact(Schema):
    label: str
    value: Value

    def __init__(self, label, value, **data):
        super().__init__(label=label, value=value, **data)


class Facts(PageBlock):
    """The label-and-value list."""

    block: Literal["facts"] = "facts"
    title: str = ""
    facts: list[Fact] = Field(default_factory=list)

    def __init__(self, facts=(), **data):
        super().__init__(facts=facts, **data)

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        for fact in self.facts:
            fact.value.check_placement(followed=followed, regions=regions, region=region)

    def to_markdown(
        self, *, app: str = "", pages: Sequence["PageRoute"] = (), endpoint: str = ""
    ) -> str:
        facts = (
            (fact.label, fact.value.to_markdown(app=app, pages=pages, endpoint=endpoint))
            for fact in self.facts
        )
        return _titled(self.title, _bullets(f"**{label}**: {value}" for label, value in facts))


class TableColumn(Schema):
    label: str
    align: Literal["start", "end"] = "start"

    def __init__(self, label, **data):
        super().__init__(label=label, **data)


class TableRow(Schema):
    cells: list[Value] = Field(default_factory=list)
    detail: str = ""
    # Identity the shell sends when this row is selected.
    key: str = ""

    def __init__(self, cells=(), **data):
        super().__init__(cells=cells, **data)


class Table(PageBlock):
    """Rows of values under named columns. Every row carries one cell for each
    column; with no rows the shell shows ``empty``. ``select`` names the
    argument the selected keys fill, and ``actions`` are what run on that
    list."""

    block: Literal["table"] = "table"
    title: str = ""
    columns: list[TableColumn] = Field(default_factory=list)
    rows: list[TableRow] = Field(default_factory=list)
    empty: EmptyState | None = None
    select: str = ""
    actions: list[Action] = Field(default_factory=list)

    @model_validator(mode="after")
    def _rows_match_the_columns(self) -> "Table":
        wrong = [len(row.cells) for row in self.rows if len(row.cells) != len(self.columns)]
        if wrong:
            raise ValueError(
                f"table {self.title!r} has rows of {sorted(set(wrong))} cells under "
                f"{len(self.columns)} columns."
            )
        return self

    @model_validator(mode="after")
    def _select_and_actions_go_together(self) -> "Table":
        if self.actions and not self.select:
            raise ValueError(
                f"table {self.title!r} has actions and no select. Name the argument "
                "the selected keys fill."
            )
        if self.select and not self.actions:
            raise ValueError(
                f"table {self.title!r} names select and has no actions. Give it "
                "actions, or drop select."
            )
        if not self.select:
            return self
        if not all(row.key for row in self.rows):
            raise ValueError(
                f"table {self.title!r} can select rows, and a row has no key. Give each row a key."
            )
        keys = [row.key for row in self.rows]
        repeated = sorted({key for key in keys if keys.count(key) > 1})
        if repeated:
            raise ValueError(
                f"table {self.title!r} has two rows keyed {repeated}; the shell "
                "would not know which one the operator picked. Give each row its "
                "own key."
            )
        for action in self.actions:
            if action.fields:
                raise ValueError(
                    f"table {self.title!r} action {action.label!r} collects fields. "
                    "A table action sends only the selected keys. Remove its fields."
                )
            if self.select in action.arguments:
                raise ValueError(
                    f"table {self.title!r} select {self.select!r} is already an "
                    f"argument on action {action.label!r}. Send each value once."
                )
        return self

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        for action in self.actions:
            action.check_placement(followed=followed, regions=regions, region=region)
        for row in self.rows:
            for cell in row.cells:
                cell.check_placement(followed=followed, regions=regions, region=region)
        if self.empty:
            self.empty.check_placement(followed=followed, regions=regions, region=region)

    def to_markdown(
        self, *, app: str = "", pages: Sequence["PageRoute"] = (), endpoint: str = ""
    ) -> str:
        # An action has no text form, so a table that selects rows has none either.
        actions = _controls(self.actions, app=app, pages=pages, endpoint=endpoint)
        body = ""
        if self.rows:
            cells = [
                [cell.to_markdown(app=app, pages=pages, endpoint=endpoint) for cell in row.cells]
                for row in self.rows
            ]
            body = _table(
                [column.label for column in self.columns],
                cells,
                [column.align for column in self.columns],
            )
        elif self.empty:
            body = self.empty.to_markdown(app=app, pages=pages, endpoint=endpoint)
        return _titled(self.title, _join([actions, body]))


class List(PageBlock):
    block: Literal["list"] = "list"
    title: str = ""
    items: list[Value] = Field(default_factory=list)

    def __init__(self, items=(), **data):
        super().__init__(items=items, **data)

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        for item in self.items:
            item.check_placement(followed=followed, regions=regions, region=region)

    def to_markdown(
        self, *, app: str = "", pages: Sequence["PageRoute"] = (), endpoint: str = ""
    ) -> str:
        return _titled(
            self.title,
            _bullets(
                item.to_markdown(app=app, pages=pages, endpoint=endpoint) for item in self.items
            ),
        )


class Stack(BlockParent):
    """Blocks down the page."""

    block: Literal["stack"] = "stack"
    gap: Literal["small", "medium", "large"] = "medium"

    def __init__(self, blocks=(), **data):
        super().__init__(blocks=blocks, **data)


class Columns(BlockParent):
    """Blocks across the page. ``even`` shares the width. ``sidebar`` keeps
    the last column a rail. ``split`` is the page: two independently scrolling
    panes, a list and what it opened. They stack on a narrow screen."""

    block: Literal["columns"] = "columns"
    layout: Literal["even", "sidebar", "split"] = "even"

    def __init__(self, blocks=(), **data):
        super().__init__(blocks=blocks, **data)

    @model_validator(mode="after")
    def _split_is_two_panes(self) -> "Columns":
        if self.layout == "split" and len(self.blocks) != 2:
            raise ValueError(
                "Columns layout='split' takes exactly two panes — a list and what it opened."
            )
        return self


class Card(BlockParent):
    """A titled panel. ``link`` is its destination. The shell makes the whole
    panel the control when ``controls`` is empty. ``drag`` is what a drop
    action receives; empty means the card does not move."""

    block: Literal["card"] = "card"
    title: str = ""
    description: str = ""
    controls: list[Action | Link] = Field(default_factory=list)
    link: Link | None = None
    drag: dict[str, Any] = Field(default_factory=dict)

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        super().check_placement(followed=followed, regions=regions, region=region)
        for control in self.controls:
            control.check_placement(followed=followed, regions=regions, region=region)

    def to_markdown(
        self, *, app: str = "", pages: Sequence["PageRoute"] = (), endpoint: str = ""
    ) -> str:
        return _join(
            [
                _heading(_linked(self.title, self.link, app=app, pages=pages, endpoint=endpoint)),
                self.description,
                super().to_markdown(app=app, pages=pages, endpoint=endpoint),
                _controls(self.controls, app=app, pages=pages, endpoint=endpoint),
            ]
        )


class Cards(PageBlock):
    """One card for each of a set of things. ``wrap`` lets the shell fit as
    many across as the screen takes. ``stack`` is one column. ``tiles`` is a
    wrap of squares: an image fills each card and is cropped rather than shown
    in full. ``drop`` is the action a dragged card submits onto this list."""

    block: Literal["cards"] = "cards"
    title: str = ""
    cards: list[Card] = Field(default_factory=list)
    empty: EmptyState | None = None
    layout: Literal["wrap", "stack", "tiles"] = "wrap"
    drop: Action | None = None

    @model_validator(mode="after")
    def _drop_is_immediate(self) -> "Cards":
        if self.drop and (self.drop.fields or self.drop.confirm):
            raise ValueError("Cards.drop cannot collect fields or confirm — the drop is the submit")
        return self

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        if self.drop:
            self.drop.check_placement(followed=followed, regions=regions, region=region)
        for card in self.cards:
            card.check_placement(followed=followed, regions=regions, region=region)
        if self.empty:
            self.empty.check_placement(followed=followed, regions=regions, region=region)

    def to_markdown(
        self, *, app: str = "", pages: Sequence["PageRoute"] = (), endpoint: str = ""
    ) -> str:
        # A dragged card runs the drop, and an action has no text form.
        drop = ""
        if self.drop:
            drop = self.drop.to_markdown(app=app, pages=pages, endpoint=endpoint)
        body = ""
        if self.cards:
            body = _join(
                card.to_markdown(app=app, pages=pages, endpoint=endpoint) for card in self.cards
            )
        elif self.empty:
            body = self.empty.to_markdown(app=app, pages=pages, endpoint=endpoint)
        return _titled(self.title, _join([drop, body]))


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

    @model_validator(mode="after")
    def _named_when_followed(self) -> "Section":
        if self.follows and not self.name:
            raise ValueError(
                "a Section that follows a subject needs a name — the shell replaces it by name"
            )
        return self

    def to_markdown(
        self, *, app: str = "", pages: Sequence["PageRoute"] = (), endpoint: str = ""
    ) -> str:
        heading = ""
        if self.title:
            heading = f"## {self.title}"
        return _join(
            [
                heading,
                _controls(self.controls, app=app, pages=pages, endpoint=endpoint),
                super().to_markdown(app=app, pages=pages, endpoint=endpoint),
            ]
        )


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

_CONTEXT_BLOCKS = TypeAdapter(list[Block])


def list_context_blocks(context: str | list[Block] | list[dict]) -> list[Block]:
    """The blocks of a review's context, as the app gives them or as the ask stores
    them. A context of text holds none. Raises ``ValidationError`` for a part that is
    not a block."""
    if isinstance(context, str):
        return []
    return _CONTEXT_BLOCKS.validate_python(context)
