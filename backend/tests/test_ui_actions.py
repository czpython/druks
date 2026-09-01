import pytest
from druks.apps.exceptions import AppRouteConflict
from druks.apps.loader import load_app
from druks.apps.schemas import Operation
from druks.ui import (
    Action,
    Card,
    EmptyState,
    Form,
    Link,
    Page,
    SecretField,
    Section,
    TextField,
)
from druks.ui.fields import PageField
from fastapi import APIRouter

OPERATIONS = {
    "write_note": Operation(id="write_note", method="POST", path="/api/field_notes/notes"),
    "read_note": Operation(id="read_note", method="GET", path="/api/field_notes/notes/1"),
}


def wire(*blocks):
    return Page("x", blocks=list(blocks)).model_dump(by_alias=True, mode="json")["blocks"]


def test_an_action_carries_its_operation_and_what_happens_next():
    (block,) = wire(
        Action(
            label="Archive",
            operation="write_note",
            arguments={"note_id": 7},
            tone="danger",
            confirm="Archive this note?",
            refresh="page",
        )
    )

    assert block == {
        "block": "action",
        "label": "Archive",
        "operation": "write_note",
        "arguments": {"note_id": 7},
        "fields": [],
        "tone": "danger",
        "confirm": "Archive this note?",
        "refresh": "page",
        "link": None,
    }


def test_a_form_carries_its_fields_and_the_action_that_sends_them():
    (block,) = wire(
        Form(
            action=Action(label="Save", operation="write_note", tone="primary"),
            title="New note",
            fields=[TextField(name="body", label="Note", is_required=True)],
        )
    )

    assert block["fields"] == [
        {
            "field": "text",
            "name": "body",
            "label": "Note",
            "value": "",
            "placeholder": "",
            "helpText": "",
            "isRequired": True,
        }
    ]
    assert block["action"]["operation"] == "write_note"
    assert "presentation" not in block


def test_an_action_can_collect_fields_before_it_runs():
    (block,) = wire(
        Action(
            label="Write a note",
            operation="write_note",
            fields=[TextField(name="body", label="Note", is_required=True)],
        )
    )

    assert block["label"] == "Write a note"
    assert block["fields"][0]["name"] == "body"


def test_an_action_cannot_collect_fields_and_ask_for_confirmation():
    with pytest.raises(ValueError, match="gives both fields and confirm"):
        Action(
            label="Write a note",
            operation="write_note",
            fields=[TextField(name="body", label="Note")],
            confirm="Write this note?",
        )


def test_an_action_sends_each_value_once():
    with pytest.raises(ValueError, match="two fields named"):
        Action(
            label="Save",
            operation="write_note",
            fields=[TextField(name="body", label="A"), TextField(name="body", label="B")],
        )
    with pytest.raises(ValueError, match="already carries as arguments"):
        Action(
            label="Save",
            operation="write_note",
            arguments={"body": "x"},
            fields=[TextField(name="body", label="A")],
        )


def test_a_secret_field_declares_no_value():
    (block,) = wire(
        Form(
            action=Action(label="Connect", operation="write_note", tone="primary"),
            fields=[
                SecretField(
                    name="token",
                    label="Access token",
                    help_text="From your account settings.",
                )
            ],
        )
    )

    assert block["fields"] == [
        {
            "field": "secret",
            "name": "token",
            "label": "Access token",
            "helpText": "From your account settings.",
            "isRequired": False,
        }
    ]


def test_the_shared_base_has_no_value_to_hand_out():
    """An upload and a secret have none. A value on the base would give every
    secret field somewhere to carry a stored secret back to the browser."""
    assert "value" not in PageField.model_fields


def test_a_form_sends_each_value_once():
    with pytest.raises(ValueError, match="two fields named"):
        Form(
            action=Action(label="Save", operation="write_note"),
            fields=[TextField(name="body", label="A"), TextField(name="body", label="B")],
        )
    with pytest.raises(ValueError, match="already"):
        Form(
            action=Action(label="Save", operation="write_note", arguments={"body": "x"}),
            fields=[TextField(name="body", label="A")],
        )


def test_a_form_keeps_all_fields_on_the_form():
    with pytest.raises(ValueError, match="Put all form fields on the form"):
        Form(
            action=Action(
                label="Save",
                operation="write_note",
                fields=[TextField(name="tag", label="Tag")],
            ),
            fields=[TextField(name="body", label="Body")],
        )


def check(page: Page) -> None:
    for action in page.iter_actions():
        action.check_operation("field_notes", OPERATIONS)


def test_an_action_must_name_one_of_the_app_operations():
    with pytest.raises(ValueError, match="which none of its routes declares"):
        check(Page("x", blocks=[Action(label="Go", operation="nowhere")]))


def test_a_get_route_can_never_be_an_action():
    with pytest.raises(ValueError, match="a GET is a read|a GET route"):
        check(Page("x", blocks=[Action(label="Go", operation="read_note")]))


def test_every_action_on_the_page_is_checked():
    page = Page(
        "x",
        controls=[Action(label="Write", operation="write_note")],
        blocks=[
            Card(
                blocks=[
                    Form(
                        action=Action(label="Save", operation="write_note"),
                        fields=[TextField(name="body", label="B")],
                    )
                ],
                controls=[
                    Action(label="Archive", operation="write_note"),
                    Link("Home", url="/"),
                ],
            ),
            EmptyState("none", controls=[Action(label="Add", operation="nowhere")]),
        ],
    )

    assert [action.operation for action in page.iter_actions()] == [
        "write_note",
        "write_note",
        "write_note",
        "nowhere",
    ]
    with pytest.raises(ValueError, match="nowhere"):
        check(page)


def test_the_app_names_each_operation_once_with_its_method_and_path():
    app = load_app("field_notes")

    operations = app.operations()

    assert operations["write_note"] == Operation(
        id="write_note", method="POST", path="/api/field_notes/notes"
    )
    assert operations["clear_gist"].path == "/api/field_notes/notes/{note_id}/gist"


def test_two_routes_cannot_share_one_operation(monkeypatch):
    async def stub() -> dict[str, str]:
        return {}

    app = load_app("field_notes")
    router = APIRouter(prefix="/duplicates")
    router.post("/one", operation_id="write_note")(stub)
    router.post("/two", operation_id="write_note")(stub)
    monkeypatch.setattr(app, "_declared_routers", classmethod(lambda cls, modules=None: [router]))

    with pytest.raises(AppRouteConflict, match="declares operation 'write_note' twice"):
        app.operations()


def test_an_action_that_refreshes_its_region_needs_one():
    with pytest.raises(ValueError, match="refreshes its region, and it sits in none"):
        Page("x", blocks=[Action(label="Go", operation="write_note", refresh="region")])


def test_a_named_section_is_a_region_an_action_can_refresh():
    page = Page(
        "x",
        blocks=[
            Section(
                name="decision",
                blocks=[
                    Card(controls=[Action(label="Go", operation="write_note", refresh="region")])
                ],
            )
        ],
    )

    assert [action.label for action in page.iter_actions()] == ["Go"]


def test_a_section_action_belongs_to_its_region():
    action = Action(label="Go", operation="write_note", refresh="region")
    page = Page(
        "x",
        blocks=[
            Section(
                name="decision",
                controls=[action],
                follows={"subject_type": "note", "subject_id": "1"},
            )
        ],
    )

    assert list(page.iter_actions()) == [action]
    section = page.model_dump(by_alias=True, mode="json")["blocks"][0]
    (control,) = section["controls"]
    assert control["label"] == "Go"


def test_a_page_action_cannot_refresh_a_region():
    with pytest.raises(ValueError, match="refreshes its region, and it sits in none"):
        Page(
            "x",
            controls=[Action(label="Go", operation="write_note", refresh="region")],
        )
