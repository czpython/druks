"""The form that the agent of a chat asks: its fields, which Druks reads from the agent's
text, and the person's answers to them."""

import ast
import math

from pydantic import ValidationError

from druks import ui
from druks.notifications.exceptions import InvalidChoiceError

from . import constants
from .exceptions import FormError

FORM_FIELDS = {
    field.__name__: field
    for field in (
        ui.TextField,
        ui.TextAreaField,
        ui.NumberField,
        ui.SelectField,
        ui.MultiSelectField,
        ui.RadioField,
        ui.CheckboxField,
    )
}


def parse_fields(text: str) -> list[dict]:
    """The fields of the form that ``text`` declares: one Python list of field calls, as in
    ``[TextField(name="title", label="Title")]``. Druks reads the text with ``ast`` and runs
    none of it, and Pydantic checks each field. Each field comes back as the fields of its
    model, by name."""
    if len(text) > constants.FIELDS_TEXT_LIMIT:
        raise FormError(
            f"The fields are {len(text)} characters long. Write them in "
            f"{constants.FIELDS_TEXT_LIMIT} characters or fewer."
        )
    try:
        tree = ast.parse(text, filename="fields", mode="eval")
    except SyntaxError as error:
        raise FormError(f"The fields are not valid Python: {error}.") from error
    # The parser runs out of stack on a text that nests deep.
    except (RecursionError, MemoryError) as error:
        raise FormError("The fields nest too deep. Write one list of field calls.") from error
    match tree.body:
        case ast.List(elts=[_, *_] as calls):
            fields = [get_object(call, FORM_FIELDS, depth=2).model_dump() for call in calls]
        case ast.List():
            raise FormError("The form has no fields. Give at least one field.")
        case _:
            raise FormError(
                'Write the fields as one list, such as [TextField(name="title", label="Title")].'
            )
    names = [field["name"] for field in fields]
    for field in fields:
        if names.count(field["name"]) > 1:
            raise FormError(
                f"Two fields have the name {field['name']!r}. Give each field its own name."
            )
        check_field(field)
    return fields


def get_object(node: ast.expr, models: dict, depth: int) -> ui.Option | ui.Field:
    """The field or the option that the call ``node`` makes: one of ``models``, called by
    its bare name or with the ``ui.`` prefix. Only an option takes a positional argument,
    its label. Pydantic checks the arguments."""
    match node:
        case ast.Call(func=ast.Name(id=name) | ast.Attribute(value=ast.Name(id="ui"), attr=name)):
            pass
        case _:
            raise FormError(
                "Call each field and option by its name, such as TextField(...) or ui.Option(...)."
            )
    if name in constants.REFUSED_FIELDS:
        raise FormError(constants.REFUSED_FIELDS[name])
    if name not in models:
        raise FormError(f"{name} is not one of {', '.join(models)}.")
    model = models[name]
    positions = ["label"] if model is ui.Option else []
    if len(node.args) > len(positions):
        raise FormError(
            f'{name} takes its arguments by name, such as name="title". Only an option takes '
            'its label without a name, as in Option("Staging", value="staging").'
        )
    arguments = {
        position: get_value(argument, depth + 1)
        for position, argument in zip(positions, node.args, strict=False)
    }
    for keyword in node.keywords:
        if keyword.arg not in model.model_fields:
            raise FormError(
                f"{name} takes no argument {keyword.arg or '**'}. "
                f"It takes {', '.join(model.model_fields)}."
            )
        if keyword.arg in arguments:
            raise FormError(f"{name} takes {keyword.arg} once.")
        arguments[keyword.arg] = get_value(keyword.value, depth + 1)
    try:
        return model.model_validate(arguments)
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(map(str, detail['loc']))}: {detail['msg']}" for detail in error.errors()
        )
        raise FormError(f"{name}: {details}.") from error


def get_value(node: ast.expr, depth: int):
    """The value of the argument ``node``: a literal, a signed number, a list of values, or
    an option."""
    if depth > constants.FIELDS_DEPTH_LIMIT:
        raise FormError(
            f"The fields nest deeper than {constants.FIELDS_DEPTH_LIMIT} levels: the list, a "
            "field, its options, an option, and its literals."
        )
    match node:
        case ast.Constant(value=str() | bool() | None):
            return node.value
        case (
            ast.Constant(value=int() | float() as number)
            | ast.UnaryOp(op=ast.UAdd(), operand=ast.Constant(value=int() | float() as number))
        ) if abs(number) < math.inf:
            return number
        case ast.UnaryOp(op=ast.USub(), operand=ast.Constant(value=int() | float() as number)) if (
            abs(number) < math.inf
        ):
            return -number
        case ast.List():
            return [get_value(element, depth + 1) for element in node.elts]
        case ast.Call():
            return get_object(node, {"Option": ui.Option}, depth)
    raise FormError(
        "The fields hold only literals, lists, and calls of the fields and Option, and Druks "
        f"runs no code. Replace the {type(node).__name__} at line {node.lineno}, column "
        f"{node.col_offset + 1}."
    )


def check_field(field: dict) -> None:
    """Refuse a field that a person cannot answer as written: a choice with no options or
    with two options of one value, a default that is not an option, a step of 0 or less, a
    minimum above the maximum, or a number default that the field refuses."""
    name = field["name"]
    match field:
        case {"options": []}:
            raise FormError(f"Field {name!r} has no options. Give it at least one Option.")
        case {"options": options, "value": value}:
            offered = {option["value"] for option in options}
            if len(offered) < len(options):
                raise FormError(
                    f"Field {name!r} has two options with one value. Give each option its "
                    "own value."
                )
            if field["field"] == "multi_select":
                chosen = set(value)
            else:
                chosen = {value} - {""}
            if not chosen <= offered:
                raise FormError(f"Field {name!r} has a value that is not one of its options.")
        case {"field": "number", "minimum": minimum, "step": step, "value": value}:
            if step is not None and step <= 0:  # ast-grep-ignore: rule-2
                raise FormError(f"Field {name!r} has the step {step}. Give a step above 0.")
            # A field takes its minimum, unless the minimum is above the maximum.
            # ast-grep-ignore: rule-2
            if minimum is not None and not is_number_taken(field, minimum):
                raise FormError(f"Field {name!r} has a minimum above its maximum.")
            if value is not None and not is_number_taken(field, value):  # ast-grep-ignore: rule-2
                raise FormError(
                    f"Field {name!r} has the value {value}, which its minimum, maximum, or "
                    "step refuses."
                )


def is_number_taken(field: dict, number: float) -> bool:
    """Whether the number field takes ``number``: a finite number, not below its minimum,
    not above its maximum, and a whole number of steps from its minimum, or from 0 with no
    minimum."""
    minimum, maximum, step = field["minimum"], field["maximum"], field["step"]
    if not math.isfinite(number):
        return False
    if minimum is not None and number < minimum:  # ast-grep-ignore: rule-2
        return False
    if maximum is not None and number > maximum:  # ast-grep-ignore: rule-2
        return False
    if step:
        steps = (number - (minimum or 0)) / step
        return math.isclose(steps, round(steps), abs_tol=1e-9)
    return True


def get_answer(fields: list[dict], values: dict) -> str:
    """The person's answers to the form as their message: one line for each field, in the
    form's order, with its label and its answer. ``values`` holds what the person gave, by
    field name: the text of a text or number field, the value of the chosen option of a
    select or a radio, the values of a multi-select's chosen options, and whether a
    checkbox is checked. A value that its field refuses raises InvalidChoiceError, which
    tells the person what to fix."""
    lines = []
    for field in fields:
        label, value = field["label"], values.get(field["name"])
        match field:
            case {"field": "checkbox"}:
                if field["is_required"] and not value:
                    raise InvalidChoiceError(f'Check "{label}".')
                answer = constants.UNCHECKED_ANSWER
                if value:
                    answer = constants.CHECKED_ANSWER
            case {"options": options}:
                if field["field"] == "multi_select":
                    chosen = set(value or [])
                else:
                    chosen = {value} - {"", None}
                if not chosen <= {option["value"] for option in options}:
                    raise InvalidChoiceError(f'Choose "{label}" from its options.')
                answer = ", ".join(
                    option["label"] for option in options if option["value"] in chosen
                )
            case {"field": "number"}:
                answer = (value or "").strip()
                try:
                    is_taken = not answer or is_number_taken(field, float(answer))
                except ValueError:
                    is_taken = False
                if not is_taken:
                    limits = [
                        f"The {limit} is {str(field[limit]).removesuffix('.0')}."
                        for limit in ("minimum", "maximum", "step")
                        if field[limit] is not None  # ast-grep-ignore: rule-2
                    ]
                    raise InvalidChoiceError(" ".join([f'Write a number in "{label}".', *limits]))
            case _:
                answer = (value or "").strip()
        if field["is_required"] and not answer:
            raise InvalidChoiceError(f'Answer "{label}".')
        lines.append(f"{label}: {answer or constants.EMPTY_ANSWER}")
    return "\n".join(lines)
