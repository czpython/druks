from typing import Annotated, Literal

from pydantic import Discriminator

from druks.schemas import Schema


class Option(Schema):
    value: str
    label: str

    def __init__(self, label: str, **data):
        super().__init__(label=label, **data)


class TextField(Schema):
    field: Literal["text"] = "text"
    name: str
    label: str
    value: str = ""
    placeholder: str = ""
    help_text: str = ""
    is_required: bool = False


class TextAreaField(Schema):
    field: Literal["text_area"] = "text_area"
    name: str
    label: str
    value: str = ""
    placeholder: str = ""
    help_text: str = ""
    is_required: bool = False
    rows: int = 4


class NumberField(Schema):
    field: Literal["number"] = "number"
    name: str
    label: str
    value: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    help_text: str = ""
    is_required: bool = False


class SelectField(Schema):
    field: Literal["select"] = "select"
    name: str
    label: str
    options: list[Option] = []
    value: str = ""
    help_text: str = ""
    is_required: bool = False


class MultiSelectField(Schema):
    field: Literal["multi_select"] = "multi_select"
    name: str
    label: str
    options: list[Option] = []
    value: list[str] = []
    help_text: str = ""
    is_required: bool = False


class RadioField(Schema):
    field: Literal["radio"] = "radio"
    name: str
    label: str
    options: list[Option] = []
    value: str = ""
    help_text: str = ""
    is_required: bool = False


class CheckboxField(Schema):
    field: Literal["checkbox"] = "checkbox"
    name: str
    label: str
    value: bool = False
    help_text: str = ""
    is_required: bool = False


class UploadField(Schema):
    """One file the operator picks. The shell stores it through the platform and
    submits its id, so the operation takes a plain string."""

    field: Literal["upload"] = "upload"
    name: str
    label: str
    # Straight into the file dialog's own filter, in its own syntax:
    # "image/*", ".csv,.tsv". It narrows what the operator sees. It is not a
    # promise about the bytes.
    accept: str = ""
    help_text: str = ""
    is_required: bool = False


class SecretField(Schema):
    """A secret the operator hands over — a token, a key. It carries no
    ``value``, the way ``UploadField`` carries none: a file input cannot be
    seeded, and a secret must not be, so an app can never echo a stored secret
    back to the browser by declaring one. The shell masks the input and keeps it
    from the password manager, and the successful-submit reset clears it."""

    field: Literal["secret"] = "secret"
    name: str
    label: str
    help_text: str = ""
    is_required: bool = False


Field = Annotated[
    TextField
    | TextAreaField
    | NumberField
    | SelectField
    | MultiSelectField
    | RadioField
    | CheckboxField
    | UploadField
    | SecretField,
    Discriminator("field"),
]
