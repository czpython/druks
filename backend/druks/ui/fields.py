from typing import Annotated, Literal

from pydantic import Discriminator

from druks.schemas import Schema


class Option(Schema):
    value: str
    label: str

    def __init__(self, label: str, **data):
        super().__init__(label=label, **data)


class PageField(Schema):
    """What every field shares. ``field`` names its kind on the wire, and
    ``name`` is the key the shell sends the operator's answer under."""

    field: str
    name: str
    label: str
    help_text: str = ""
    is_required: bool = False
    # No ``value`` here: an upload and a secret have none, and a field with
    # nowhere to hold one cannot send a stored secret to the browser.


class TextField(PageField):
    field: Literal["text"] = "text"
    value: str = ""
    placeholder: str = ""


class TextAreaField(PageField):
    field: Literal["text_area"] = "text_area"
    value: str = ""
    placeholder: str = ""
    rows: int = 4


class NumberField(PageField):
    field: Literal["number"] = "number"
    value: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None


class SelectField(PageField):
    field: Literal["select"] = "select"
    options: list[Option] = []
    value: str = ""


class MultiSelectField(PageField):
    field: Literal["multi_select"] = "multi_select"
    options: list[Option] = []
    value: list[str] = []


class RadioField(PageField):
    field: Literal["radio"] = "radio"
    options: list[Option] = []
    value: str = ""


class CheckboxField(PageField):
    field: Literal["checkbox"] = "checkbox"
    value: bool = False


class UploadField(PageField):
    """One file the operator picks. The shell stores it through the platform and
    submits its id, so the operation takes a plain string."""

    field: Literal["upload"] = "upload"
    # Straight into the file dialog's own filter, in its own syntax:
    # "image/*", ".csv,.tsv". It narrows what the operator sees. It is not a
    # promise about the bytes.
    accept: str = ""


class MultiUploadField(PageField):
    """Several files the operator picks. The shell stores each file and
    submits their ids, so the operation takes a plain list of strings."""

    field: Literal["multi_upload"] = "multi_upload"
    # The file dialog uses this filter, for example "image/*" or ".csv,.tsv".
    # It narrows the choices, but does not validate the bytes.
    accept: str = ""


class SecretField(PageField):
    """One secret the operator hands over: a token, a key. It has no ``value``,
    so a page cannot send a stored secret back to the browser."""

    field: Literal["secret"] = "secret"


Field = Annotated[
    TextField
    | TextAreaField
    | NumberField
    | SelectField
    | MultiSelectField
    | RadioField
    | CheckboxField
    | UploadField
    | MultiUploadField
    | SecretField,
    Discriminator("field"),
]
