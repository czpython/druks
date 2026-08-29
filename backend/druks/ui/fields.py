from typing import Annotated, Literal

from pydantic import Discriminator

from druks.schemas import BaseResponse


class Option(BaseResponse):
    value: str
    label: str

    def __init__(self, label: str, **data):
        super().__init__(label=label, **data)


class TextField(BaseResponse):
    field: Literal["text"] = "text"
    name: str
    label: str
    value: str = ""
    placeholder: str = ""
    help_text: str = ""
    is_required: bool = False


class TextAreaField(BaseResponse):
    field: Literal["text_area"] = "text_area"
    name: str
    label: str
    value: str = ""
    placeholder: str = ""
    help_text: str = ""
    is_required: bool = False
    rows: int = 4


class NumberField(BaseResponse):
    field: Literal["number"] = "number"
    name: str
    label: str
    value: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    help_text: str = ""
    is_required: bool = False


class SelectField(BaseResponse):
    field: Literal["select"] = "select"
    name: str
    label: str
    options: list[Option] = []
    value: str = ""
    help_text: str = ""
    is_required: bool = False


class MultiSelectField(BaseResponse):
    field: Literal["multi_select"] = "multi_select"
    name: str
    label: str
    options: list[Option] = []
    value: list[str] = []
    help_text: str = ""
    is_required: bool = False


class RadioField(BaseResponse):
    field: Literal["radio"] = "radio"
    name: str
    label: str
    options: list[Option] = []
    value: str = ""
    help_text: str = ""
    is_required: bool = False


class CheckboxField(BaseResponse):
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
