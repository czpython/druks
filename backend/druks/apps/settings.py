from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel, SecretStr, ValidationError
from pydantic.fields import FieldInfo

from .exceptions import SettingsDeclarationError


@dataclass(frozen=True)
class Choices:
    """Live choices for a ``str`` setting, declared as ``Annotated[str, Choices(source)]``.
    ``source`` returns ``(stored value, label)`` pairs."""

    source: Callable[[], Awaitable[list[tuple[str, str]]]]


# The annotation picks the wire kind, and the frontend picks the input control from it.
_SCALAR_KINDS: dict[type, str] = {bool: "bool", int: "int", str: "str"}


def _is_secret_annotation(annotation: object) -> bool:
    # SecretStr anywhere in the annotation marks the field secret, also inside a union or a
    # container. A secret then cannot leak as plain text from any declared shape.
    if annotation is SecretStr:
        return True
    return any(_is_secret_annotation(arg) for arg in get_args(annotation))


def _literal_members(annotation: object) -> tuple[Any, ...] | None:
    # The members of a Literal, also inside a union such as ``Literal["a"] | None``.
    # Members keep their declared type.
    if get_origin(annotation) is Literal:
        return get_args(annotation)
    members = [member for arg in get_args(annotation) for member in (_literal_members(arg) or ())]
    return tuple(members) if members else None


def field_kind(field: FieldInfo) -> str:
    annotation = field.annotation
    if _is_secret_annotation(annotation):
        return "secret"
    if _literal_members(annotation):
        return "enum"
    if isinstance(annotation, type):
        return _SCALAR_KINDS.get(annotation, "str")
    return "str"


def field_choices(field: FieldInfo) -> list[str] | None:
    # The wire carries choices as strings. coerce_setting_value maps a submitted string
    # back to the declared member.
    if members := _literal_members(field.annotation):
        return [str(member) for member in members]


def _nests_choices(annotation: object) -> bool:
    # Pydantic moves Choices into the field metadata only from the outermost Annotated.
    return any(isinstance(arg, Choices) or _nests_choices(arg) for arg in get_args(annotation))


def field_choice_source(field: FieldInfo) -> Callable[[], Awaitable[list[tuple[str, str]]]] | None:
    return next((item.source for item in field.metadata if isinstance(item, Choices)), None)


def field_section(field: FieldInfo) -> str:
    metadata = field.json_schema_extra
    if isinstance(metadata, dict):
        return str(metadata.get("section", ""))
    return ""


def field_multiline(field: FieldInfo) -> bool:
    # A pasted value with newlines, such as a PEM key, gets a textarea. Storage is the same.
    metadata = field.json_schema_extra
    if isinstance(metadata, dict):
        return bool(metadata.get("multiline", False))
    return False


def validate_field_choice_details(field: FieldInfo) -> dict[str, dict[str, str]]:
    # A key outside the Literal shows as a raw value in the form. Druks rejects it here.
    metadata = field.json_schema_extra
    details: dict[str, dict[str, str]] = {}
    if isinstance(metadata, dict):
        details = metadata.get("choice_details", {})
    choices = field_choices(field) or []
    unknown = sorted(set(details) - set(choices))
    if unknown:
        raise SettingsDeclarationError(
            f"choice_details keys {unknown!r} are not declared choices. "
            f"Use values from {choices!r}."
        )
    return details


def field_visibility(field: FieldInfo) -> tuple[str, Any]:
    # The sibling field and the values that show this field. An empty name means always shown.
    metadata = field.json_schema_extra
    if isinstance(metadata, dict):
        condition = metadata.get("visible_when")
        if isinstance(condition, dict) and len(condition) == 1:
            [(controller, targets)] = condition.items()
            return str(controller), targets
    return "", []


def _nested_model(annotation: object) -> type[BaseModel] | None:
    # The flat settings plane cannot render or key a nested model.
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in get_args(annotation):
        if nested := _nested_model(arg):
            return nested


def validate_settings_declaration(model: type[BaseModel]) -> None:
    # A bad declaration fails when the app loads, not at the first save from the settings page.
    for name, field in model.model_fields.items():
        validate_field_choice_details(field)
        source = field_choice_source(field)
        if (source and field.annotation is not str) or _nests_choices(field.annotation):
            raise SettingsDeclarationError(
                f"settings field {name!r}: Choices applies only to a str field. "
                "Declare it as Annotated[str, Choices(source)]."
            )
        if nested := _nested_model(field.annotation):
            raise SettingsDeclarationError(
                f"settings field {name!r}: nested models are not a supported settings "
                f"shape (found {nested.__name__}); declare scalar, SecretStr, or Literal fields"
            )
        _validate_visible_when(model, name, field)


def _validate_visible_when(model: type[BaseModel], name: str, field: FieldInfo) -> None:
    # The client evaluates the condition, so its controller cannot be a secret. The controller
    # cannot be conditional either, or a condition could depend on a hidden control.
    metadata = field.json_schema_extra
    has_visible_when = isinstance(metadata, dict) and "visible_when" in metadata
    controller_name, targets = field_visibility(field)
    if has_visible_when and not (controller_name and isinstance(targets, list) and targets):
        raise SettingsDeclarationError(
            f"settings field {name!r}: visible_when takes one {{field: [values]}} condition. "
            "Name one controller with a non-empty list of values."
        )
    if not controller_name:
        return
    controller = model.model_fields.get(controller_name)
    if not controller:
        raise SettingsDeclarationError(
            f"settings field {name!r}: visible_when controller {controller_name!r} is not declared"
        )
    if _is_secret_annotation(controller.annotation):
        raise SettingsDeclarationError(
            f"settings field {name!r}: visible_when controller {controller_name!r} cannot be secret"
        )
    chained, _ = field_visibility(controller)
    if chained:
        raise SettingsDeclarationError(
            f"settings field {name!r}: visible_when controller "
            f"{controller_name!r} cannot itself declare visible_when"
        )
    # ``True == 1`` in Python. The type must match too, or the client never matches the target.
    if members := _literal_members(controller.annotation):
        for target in targets:
            if not any(type(target) is type(member) and target == member for member in members):
                raise SettingsDeclarationError(
                    f"settings field {name!r}: visible_when target {target!r} is not a member of "
                    f"{controller_name!r}"
                )


def coerce_setting_value(model: type[BaseModel], field: str, value: Any) -> Any:
    # A select submits strings, but a Literal can hold ints or bools.
    if not isinstance(value, str):
        return value
    field_info = model.model_fields.get(field)
    if not field_info:
        return value
    members = _literal_members(field_info.annotation)
    if not members:
        return value
    return next((member for member in members if str(member) == value), value)


def validate_setting_override(
    model: type[BaseModel], current: dict[str, Any], field: str, value: Any
) -> None:
    # Validate the whole model with the new value, so cross-field validators see real state.
    # The error message is redacted, so a rejected secret never reaches the 422 body.
    try:
        model.model_validate({**current, field: value})
    except ValidationError as error:
        raise ValueError(_redacted_validation_message(model, error)) from error


def _redacted_validation_message(model: type[BaseModel], error: ValidationError) -> str:
    # A Pydantic error echoes the submitted value, so rebuild the message from safe keys.
    # A custom validator can put a raw value in ``msg``, so a secret field error and a
    # model-level error get a generic line.
    parts = []
    for detail in error.errors():
        location = tuple(detail["loc"])
        label = ".".join(str(part) for part in location) or "(value)"
        if _touches_secret(model, location):
            parts.append(f"{label}: invalid value")
        else:
            parts.append(f"{label}: {detail['msg']}")
    return "; ".join(parts)


def _touches_secret(model: type[BaseModel], location: tuple[Any, ...]) -> bool:
    # A model-level error has no location, and its validator can read every field. Redact
    # it when the model declares any secret.
    fields = model.model_fields
    if not location:
        return any(field_kind(info) == "secret" for info in fields.values())
    field = location[0]
    return isinstance(field, str) and field in fields and field_kind(fields[field]) == "secret"
