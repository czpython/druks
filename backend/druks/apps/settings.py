from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel, SecretStr, ValidationError
from pydantic.fields import FieldInfo

from .exceptions import SettingsDeclarationError

# A declared field's Python annotation picks its wire kind, which is the only thing
# the frontend switches on to choose an input control (checkbox / number / text /
# select / password). ``SecretStr`` and a ``Literal`` choice set are the two rich
# kinds an author reaches for beyond the scalars.
_SCALAR_KINDS: dict[type, str] = {bool: "bool", int: "int", str: "str"}


def _is_secret_annotation(annotation: object) -> bool:
    # ``SecretStr`` anywhere in the annotation tree marks the field secret — bare, in a
    # union (``SecretStr | None``), or in a container (``list[SecretStr]``) — so a
    # secret can't slip through as a plaintext value from any shape it's declared in.
    if annotation is SecretStr:
        return True
    return any(_is_secret_annotation(arg) for arg in get_args(annotation))


def _literal_members(annotation: object) -> tuple[Any, ...] | None:
    # The choices of a ``Literal`` field, unwrapping an ``Optional``/union so
    # ``Literal["a", "b"] | None`` — and a union of separate literals like
    # ``Literal["a"] | Literal["b"]`` — is still recognized. None when the field
    # declares no literal. Members keep their declared type (str, int, …).
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
    # An enum's closed choice set, surfaced so the UI renders a select. The wire is
    # always strings (the select submits ``e.target.value``); ``coerce_setting_value``
    # maps a submitted string back to the member's declared type on the way in.
    members = _literal_members(field.annotation)
    if not members:
        return None
    return [str(member) for member in members]


def field_choice_labels(field: FieldInfo) -> dict[str, str]:
    # Display wording for a subset of ``field_choices``. Omitted members render
    # as the stored value. Empty when the field has no labels.
    metadata = field.json_schema_extra
    if isinstance(metadata, dict):
        labels = metadata.get("choice_labels")
        if isinstance(labels, dict):
            return {str(key): str(value) for key, value in labels.items()}
    return {}


def field_section(field: FieldInfo) -> str:
    # The heading a field groups under; empty when it is ungrouped.
    metadata = field.json_schema_extra
    if isinstance(metadata, dict):
        return str(metadata.get("section", ""))
    return ""


def field_multiline(field: FieldInfo) -> bool:
    # A field whose pasted value carries meaningful newlines (a PEM private
    # key); the UI renders a textarea instead of a one-line input. Declared as
    # ``json_schema_extra={"multiline": True}``; presentation only — storage,
    # redaction, and write-only semantics are unchanged.
    metadata = field.json_schema_extra
    if isinstance(metadata, dict):
        return bool(metadata.get("multiline", False))
    return False


def field_visibility(field: FieldInfo) -> tuple[str, Any]:
    # The sibling field this one is shown for and the value that field must hold. The
    # name is empty when the field is always shown.
    metadata = field.json_schema_extra
    if isinstance(metadata, dict):
        condition = metadata.get("visible_when")
        if isinstance(condition, dict):
            controller, target = next(iter(condition.items()))
            return str(controller), target
    return "", None


def _nested_model(annotation: object) -> type[BaseModel] | None:
    # A ``BaseModel`` anywhere in the annotation tree — the one shape the flat settings
    # plane can't render or key by. ``SecretStr`` is a str, not a model, so it's clear.
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in get_args(annotation):
        if nested := _nested_model(arg):
            return nested
    return None


def validate_settings_declaration(model: type[BaseModel]) -> None:
    # A settings field is a scalar, a ``SecretStr``, a ``Literal``, or an Optional /
    # container of those — never a nested model. Reject a nested model at declaration so
    # a shape the plane can't render (or safely redact) fails loudly where it's written
    # rather than at the first operator PATCH.
    for name, field in model.model_fields.items():
        if nested := _nested_model(field.annotation):
            raise SettingsDeclarationError(
                f"settings field {name!r}: nested models are not a supported settings "
                f"shape (found {nested.__name__}); declare scalar, SecretStr, or Literal fields"
            )
        _validate_visible_when(model, name, field)
        _validate_choice_labels(name, field)


def _validate_visible_when(model: type[BaseModel], name: str, field: FieldInfo) -> None:
    # One equality condition against a sibling field, which the client must be able to
    # read back to evaluate it: never a secret, whose value never leaves the server, and
    # never itself conditional, which would let a condition hang off a hidden control.
    controller_name, target = field_visibility(field)
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
    members = _literal_members(controller.annotation)
    # ``True == 1`` in Python, so a target must match a member's type as well as its
    # value — otherwise it passes here and then never matches the client's comparison.
    if members and not any(type(target) is type(member) and target == member for member in members):
        raise SettingsDeclarationError(
            f"settings field {name!r}: visible_when target {target!r} is not a member of "
            f"{controller_name!r}"
        )


def _validate_choice_labels(name: str, field: FieldInfo) -> None:
    metadata = field.json_schema_extra
    if not isinstance(metadata, dict) or "choice_labels" not in metadata:
        return
    labels = metadata["choice_labels"]
    if not isinstance(labels, dict):
        raise SettingsDeclarationError(
            f"settings field {name!r}: choice_labels must be a dict of member to label"
        )
    members = _literal_members(field.annotation)
    if not members:
        raise SettingsDeclarationError(
            f"settings field {name!r}: choice_labels is only valid on a Literal field"
        )
    allowed = {str(member) for member in members}
    unknown = sorted(str(key) for key in labels if str(key) not in allowed)
    if unknown:
        raise SettingsDeclarationError(
            f"settings field {name!r}: choice_labels keys {unknown} are not members of the Literal"
        )


def coerce_setting_value(model: type[BaseModel], field: str, value: Any) -> Any:
    # A select submits every choice as a string, but a ``Literal`` may hold ints or
    # bools — map the submitted string back to the member it names so validation sees
    # the declared type. Leaves non-enum fields and already-typed values untouched.
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
    # Merge the new value onto the currently-resolved settings and validate the whole
    # model, so cross-field validators run against real state (not a blank shell of
    # defaults). ``current`` is already a valid, resolved settings dump, so a sibling
    # can't spuriously fail. On failure, raise a ValueError whose message is redacted —
    # never the submitted input, and never a secret field's raw value — so a rejected
    # secret can't ride out in the 422 body.
    try:
        model.model_validate({**current, field: value})
    except ValidationError as error:
        raise ValueError(_redacted_validation_message(model, error)) from error


def _redacted_validation_message(model: type[BaseModel], error: ValidationError) -> str:
    # Pydantic's ``str(ValidationError)`` (and each error's ``input``/``ctx``/``url``)
    # echoes the submitted value — a secret leak — so rebuild from the safe keys only.
    # ``msg`` is safe for a built-in error, but a custom validator can embed the raw
    # value in it: drop ``msg`` for a secret field's error (and any model-level error,
    # where a validator saw every field, secrets included) in favor of a generic line.
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
    # A field-level error names its field first; redact when that field is a secret. A
    # model-level error has an empty location — a model validator can read every field,
    # so redact whenever the model declares any secret at all.
    fields = model.model_fields
    if not location:
        return any(field_kind(info) == "secret" for info in fields.values())
    field = location[0]
    return isinstance(field, str) and field in fields and field_kind(fields[field]) == "secret"
