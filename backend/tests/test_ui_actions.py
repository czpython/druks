import pytest
from druks.apps.exceptions import AppRouteConflict
from druks.apps.loader import load_app
from druks.apps.schemas import Operation
from druks.ui import Action, Card, EmptyState, Form, Link, Page, Section, TextField

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
        blocks=[
            Card(
                blocks=[
                    Form(
                        action=Action(label="Save", operation="write_note"),
                        fields=[TextField(name="body", label="B")],
                    )
                ],
                actions=[
                    Action(label="Archive", operation="write_note"),
                    Link("Home", url="/"),
                ],
            ),
            EmptyState("none", actions=[Action(label="Add", operation="nowhere")]),
        ],
    )

    assert [action.operation for action in page.iter_actions()] == [
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
    from fastapi import APIRouter

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
                    Card(actions=[Action(label="Go", operation="write_note", refresh="region")])
                ],
            )
        ],
    )

    assert [action.label for action in page.iter_actions()] == ["Go"]
