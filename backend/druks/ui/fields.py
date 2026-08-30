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


Field = Annotated[
    TextField
    | TextAreaField
    | NumberField
    | SelectField
    | MultiSelectField
    | RadioField
    | CheckboxField,
    Discriminator("field"),
]
