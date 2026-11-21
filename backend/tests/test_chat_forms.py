import re

import pytest
from druks import ui
from druks.chat.constants import FIELDS_TEXT_LIMIT
from druks.chat.exceptions import FormError
from druks.chat.forms import parse_fields

EXAMPLE = (
    '[TextField(name="title", label="Title"), SelectField(name="where", label="Where", '
    'options=[Option("Staging", value="staging")])]'
)


def test_the_ticket_s_example_reads_as_its_two_fields():
    assert parse_fields(EXAMPLE) == [
        {
            "field": "text",
            "name": "title",
            "label": "Title",
            "help_text": "",
            "is_required": False,
            "value": "",
            "placeholder": "",
        },
        {
            "field": "select",
            "name": "where",
            "label": "Where",
            "help_text": "",
            "is_required": False,
            "options": [{"value": "staging", "label": "Staging", "group": ""}],
            "value": "",
        },
    ]


@pytest.mark.parametrize("prefix", ["", "ui."], ids=["bare", "ui-prefix"])
def test_a_form_reads_each_field_by_its_bare_name_or_with_the_ui_prefix(prefix):
    calls = [
        'TextField(name="title", label="Title", placeholder="A short title", is_required=True)',
        'TextAreaField(name="notes", label="Notes", help_text="Markdown is fine.", rows=3)',
        'NumberField(name="budget", label="Budget", minimum=-10, maximum=+10.5, step=0.5, '
        "value=-2)",
        f'SelectField(name="where", label="Where", options=[{prefix}Option("Staging", value="s")])',
        f'MultiSelectField(name="tags", label="Tags", options=[{prefix}Option(label="Rack", '
        'value="rack"), Option("Shelf", value="shelf")], value=["shelf"])',
        f'RadioField(name="size", label="Size", options=[{prefix}Option("Large", value="l")])',
        'CheckboxField(name="notify", label="Notify the owner", value=True)',
    ]

    fields = parse_fields("[" + ", ".join(prefix + call for call in calls) + "]")

    assert [(field["field"], field["name"]) for field in fields] == [
        ("text", "title"),
        ("text_area", "notes"),
        ("number", "budget"),
        ("select", "where"),
        ("multi_select", "tags"),
        ("radio", "size"),
        ("checkbox", "notify"),
    ]
    title, notes, budget, where, tags, size, notify = fields
    assert (title["placeholder"], title["is_required"]) == ("A short title", True)
    assert (notes["help_text"], notes["rows"]) == ("Markdown is fine.", 3)
    assert [budget[key] for key in ("minimum", "maximum", "step", "value")] == [-10, 10.5, 0.5, -2]
    assert [(option["label"], option["value"]) for option in tags["options"]] == [
        ("Rack", "rack"),
        ("Shelf", "shelf"),
    ]
    assert (where["options"][0]["value"], tags["value"], size["options"][0]["label"]) == (
        "s",
        ["shelf"],
        "Large",
    )
    assert notify["value"] is True


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ('[TextField("title", label="Title")]', "TextField takes its arguments by name"),
        (
            '[SelectField(name="s", label="S", options=[Option("A", "a")])]',
            "Only an option takes its label without a name",
        ),
        ('[TextField(name="t", **{"label": "T"})]', "TextField takes no argument **."),
        ('[TextField(*["t"])]', "TextField takes its arguments by name"),
        ('[*[TextField(name="t", label="T")]]', "Call each field and option by its name"),
        ('[EmailField(name="e", label="E")]', "EmailField is not one of TextField,"),
        ('[Option("Staging", value="staging")]', "Option is not one of TextField,"),
        ('[druks.ui.TextField(name="t", label="T")]', "Call each field and option by its name"),
        ('[TextField(name="t", label="T", name="u")]', "TextField takes name once."),
        ('[TextField(name="t", label=("T"), value="a" + "b")]', "Replace the BinOp"),
        ('[TextField(name="t", label=title)]', "Replace the Name at line 1, column 28."),
        ('[TextField(name="t", label=f"{1}")]', "Replace the JoinedStr"),
        ('[TextField(name="t", label="T", value={"a": "b"})]', "Replace the Dict"),
        ('[TextField(name="t", label=(lambda: "T")())]', "Call each field and option by its name"),
        ('[NumberField(name="n", label="N", value=1e999)]', "Replace the Constant"),
    ],
    ids=[
        "positional-field-argument",
        "second-positional-option-argument",
        "keyword-unpacking",
        "positional-unpacking",
        "list-unpacking",
        "unknown-name",
        "option-as-a-field",
        "other-qualification",
        "repeated-argument",
        "expression",
        "name",
        "f-string",
        "dict",
        "lambda",
        "infinite-number",
    ],
)
def test_a_form_refuses_what_is_not_a_literal_list_or_field_call(text, reason):
    with pytest.raises(FormError, match=re.escape(reason)):
        parse_fields(text)


def test_a_form_refuses_an_alias_and_an_extra_argument_that_the_shared_field_takes():
    for argument in ('helpText="h"', 'colour="red"'):
        with pytest.raises(FormError, match="It takes field, name, label, help_text"):
            parse_fields(f'[TextField(name="t", label="T", {argument})]')

    # Pages build the same field with both.
    field = ui.TextField.model_validate({"name": "t", "label": "T", "helpText": "h", "colour": 1})
    assert field.help_text == "h"


def test_the_fields_run_none_of_their_text(tmp_path):
    ran = tmp_path / "ran"
    text = f'[TextField(name="t", label=__import__("pathlib").Path({str(ran)!r}).touch())]'

    with pytest.raises(FormError):
        parse_fields(text)

    assert not ran.exists()


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ('[TextField(name="title")]', "TextField: label: Field required."),
        (
            '[CheckboxField(name="c", label="C", is_required="maybe")]',
            "CheckboxField: is_required: Input should be a valid boolean",
        ),
        (
            '[RadioField(name="r", label="R", options=[Option("A")])]',
            "Option: value: Field required.",
        ),
    ],
    ids=["missing-label", "wrong-type", "option-without-value"],
)
def test_pydantic_s_reason_reaches_the_agent(text, reason):
    with pytest.raises(FormError, match=re.escape(reason)):
        parse_fields(text)


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("SecretField", "A secret that a person types in Slack stays in Slack."),
        ("UploadField", "Slack shows a file input only in a modal"),
        ("MultiUploadField", "Slack shows a file input only in a modal"),
    ],
)
@pytest.mark.parametrize("prefix", ["", "ui."], ids=["bare", "ui-prefix"])
def test_a_form_refuses_a_secret_or_an_upload_with_its_reason(name, reason, prefix):
    with pytest.raises(FormError) as refused:
        parse_fields(f'[TextField(name="t", label="T"), {prefix}{name}(name="f", label="F")]')

    assert str(refused.value).startswith(f"A form takes no {name}.")
    assert reason in str(refused.value)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("[]", "The form has no fields."),
        ('TextField(name="t", label="T")', "Write the fields as one list"),
        (
            '[TextField(name="t", label="T"), CheckboxField(name="t", label="U")]',
            "Two fields have the name 't'.",
        ),
        ('[SelectField(name="s", label="S")]', "Field 's' has no options."),
        ('[MultiSelectField(name="m", label="M", options=[])]', "Field 'm' has no options."),
        ('[RadioField(name="r", label="R")]', "Field 'r' has no options."),
        (
            '[RadioField(name="r", label="R", options=[Option("A", value="a"), Option("B", '
            'value="a")])]',
            "Field 'r' has two options with one value.",
        ),
        (
            '[SelectField(name="s", label="S", options=[Option("A", value="a")], value="b")]',
            "Field 's' has a value that is not one of its options.",
        ),
        (
            '[MultiSelectField(name="m", label="M", options=[Option("A", value="a")], '
            'value=["a", "b"])]',
            "Field 'm' has a value that is not one of its options.",
        ),
        ('[NumberField(name="n", label="N", step=-1)]', "Field 'n' has the step -1.0."),
        (
            '[NumberField(name="n", label="N", minimum=5, maximum=1)]',
            "Field 'n' has a minimum above its maximum.",
        ),
        (
            '[NumberField(name="n", label="N", minimum=1, step=2, value=4)]',
            "Field 'n' has the value 4.0, which its minimum, maximum, or step refuses.",
        ),
        (
            '[NumberField(name="n", label="N", maximum=-1, value=0)]',
            "Field 'n' has the value 0.0, which its minimum, maximum, or step refuses.",
        ),
    ],
    ids=[
        "empty-form",
        "not-a-list",
        "duplicate-names",
        "select-without-options",
        "multi-select-without-options",
        "radio-without-options",
        "duplicate-option-values",
        "select-default-not-offered",
        "multi-select-default-not-offered",
        "step-below-zero",
        "minimum-above-maximum",
        "default-off-its-step",
        "default-above-its-maximum",
    ],
)
def test_a_form_refuses_fields_that_a_person_cannot_answer(text, reason):
    with pytest.raises(FormError, match=re.escape(reason)):
        parse_fields(text)


def test_a_form_takes_a_number_default_on_its_steps_from_its_minimum():
    [field] = parse_fields('[NumberField(name="n", label="N", minimum=0.5, step=1, value=1.5)]')

    assert field["value"] == 1.5


def test_a_form_reads_fields_as_long_as_the_limit_and_refuses_one_character_more():
    frame = '[TextField(name="t", label="T", help_text="")]'
    filler = "x" * (FIELDS_TEXT_LIMIT - len(frame))

    [field] = parse_fields(frame.replace('""', f'"{filler}"'))

    assert field["help_text"] == filler
    with pytest.raises(FormError, match=f"{FIELDS_TEXT_LIMIT + 1} characters long"):
        parse_fields(frame.replace('""', f'"{filler}x"'))


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ('[TextField(name="t", label="T", value=[[["x"]]])]', "The fields nest deeper than 5"),
        ("[" * 300 + "]" * 300, "too many nested parentheses"),
        ("-" * 19_000 + "1", "The fields nest too deep."),
        ('[TextField(name="t", label="T"', "The fields are not valid Python: '(' was never"),
    ],
    ids=["past-the-depth-limit", "past-python-s-nesting", "past-the-parser-s-stack", "syntax"],
)
def test_a_form_refuses_fields_that_nest_too_deep_or_are_not_python(text, reason):
    with pytest.raises(FormError, match=re.escape(reason)):
        parse_fields(text)
