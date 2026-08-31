from inspect import Parameter, Signature
from types import ModuleType

import pytest
from druks.apps.exceptions import AppRouteConflict
from druks.apps.loader import load_app
from druks.ui import Page, page
from druks.ui.exceptions import PageRouteError
from druks.ui.page import PageRoute, list_pages_for_app
from fastapi import APIRouter


def declare(
    package: str,
    path: str,
    name: str,
    *parameters: str,
    parent: PageRoute | None = None,
    label: str = "",
) -> PageRoute:
    """One page in a make-believe app package, so a test can build a table the
    loader would reject without shipping a package for each one."""

    async def project():
        return Page(title=name)

    project.__name__ = name
    project.__module__ = f"{package}.pages"
    project.__signature__ = Signature(  # type: ignore[attr-defined]
        [Parameter(parameter, Parameter.POSITIONAL_OR_KEYWORD) for parameter in parameters]
    )
    declaration = parent.child(path, label=label) if parent else page(path, label=label)
    return declaration(project)


def routes_for(package: str) -> list[str]:
    return [declaration.route for declaration in list_pages_for_app(package, package)]


def test_discovery_loads_the_pages_module():
    app = load_app("field_notes")

    assert [declaration.name for declaration in app.pages()] == [
        "notes",
        "new_note",
        "note",
        "note_history",
        "recent_notes",
    ]


def test_child_route_joins_its_parent():
    app = load_app("field_notes")
    pages = {declaration.name: declaration for declaration in app.pages()}

    assert pages["note_history"].route == "/notes/{note_id}/history"
    assert pages["note_history"].parent is pages["note"]
    # The landing page's route is "/", so its child does not double the slash.
    assert pages["recent_notes"].route == "/recent"


def test_a_static_child_is_a_tab_and_a_parameterized_page_is_not():
    app = load_app("field_notes")
    pages = {declaration.name: declaration for declaration in app.pages()}

    assert pages["note_history"].is_static
    assert pages["recent_notes"].is_static
    assert not pages["note"].is_static


def test_a_label_comes_from_the_page_name():
    app = load_app("field_notes")
    pages = {declaration.name: declaration for declaration in app.pages()}

    assert pages["note_history"].label == "note history"


def test_a_declared_label_wins():
    assert declare("labelled", "/peers", "peers", label="Peer roster").label == "Peer roster"


def test_literal_segments_match_before_parameters():
    # Declared parameter first, so only the sort can put the literal ahead of it.
    declare("precedence", "/", "overview")
    declare("precedence", "/notes/{note_id}", "note", "note_id")
    declare("precedence", "/notes/new", "new_note")

    assert routes_for("precedence") == ["/", "/notes/new", "/notes/{note_id}"]


def test_child_pages_follow_the_same_precedence():
    declare("child_precedence", "/", "overview")
    note = declare("child_precedence", "/notes/{note_id}", "note", "note_id")
    declare("child_precedence", "/{section}", "section", "note_id", "section", parent=note)
    declare("child_precedence", "/history", "note_history", "note_id", parent=note)

    assert routes_for("child_precedence") == [
        "/",
        "/notes/{note_id}",
        "/notes/{note_id}/history",
        "/notes/{note_id}/{section}",
    ]


def test_a_catch_all_matches_last():
    declare("catch_all", "/", "overview")
    declare("catch_all", "/files/{rest:path}", "any_file", "rest")
    declare("catch_all", "/files/{name}", "one_file", "name")
    declare("catch_all", "/files/recent", "recent_files")

    assert routes_for("catch_all") == [
        "/",
        "/files/recent",
        "/files/{name}",
        "/files/{rest:path}",
    ]


def test_a_child_declaration_can_live_in_another_module():
    declare("cross_module", "/", "overview")
    note = declare("cross_module", "/notes/{note_id}", "note", "note_id")

    async def note_history(note_id: int):
        return Page(title="History")

    note_history.__module__ = "cross_module.more_pages"
    child = note.child("/history")(note_history)

    assert child.route == "/notes/{note_id}/history"
    assert "/notes/{note_id}/history" in routes_for("cross_module")


def test_a_child_of_a_child_fails_at_declaration():
    note = declare("nested", "/notes/{note_id}", "note", "note_id")
    history = declare("nested", "/history", "note_history", "note_id", parent=note)

    with pytest.raises(PageRouteError, match="one child level is allowed"):
        history.child("/deeper")


def test_a_missing_landing_page_fails():
    declare("no_landing", "/notes", "notes")

    with pytest.raises(PageRouteError, match=r"declares 0 pages at '/'"):
        list_pages_for_app("no_landing", "no_landing")


def test_two_landing_pages_fail():
    declare("two_landings", "/", "overview")
    declare("two_landings", "/", "home")

    with pytest.raises(PageRouteError, match=r"declares 2 pages at '/'"):
        list_pages_for_app("two_landings", "two_landings")


def test_equivalent_parameter_shapes_fail():
    declare("same_shape", "/", "overview")
    declare("same_shape", "/{note_id}", "note", "note_id")
    declare("same_shape", "/{slug}", "note_by_slug", "slug")

    with pytest.raises(PageRouteError, match="same shape"):
        list_pages_for_app("same_shape", "same_shape")


def test_a_repeated_page_name_fails():
    declare("same_name", "/", "overview")
    declare("same_name", "/notes", "notes")

    async def notes():
        return Page(title="Notes again")

    notes.__module__ = "same_name.more_pages"
    page("/archive")(notes)

    with pytest.raises(PageRouteError, match="two pages named 'notes'"):
        list_pages_for_app("same_name", "same_name")


def test_a_child_must_take_its_parent_parameters():
    declare("dropped_parameter", "/", "overview")
    note = declare("dropped_parameter", "/notes/{note_id}", "note", "note_id")
    declare("dropped_parameter", "/history", "note_history", parent=note)

    with pytest.raises(PageRouteError, match=r"takes \[\], and its route"):
        list_pages_for_app("dropped_parameter", "dropped_parameter")


def test_an_extra_parameter_must_come_from_the_route():
    declare("extra_parameter", "/", "overview")
    declare("extra_parameter", "/notes", "notes", "note_id")

    with pytest.raises(PageRouteError, match="one parameter for each route parameter"):
        list_pages_for_app("extra_parameter", "extra_parameter")


def test_navigation_resolves_page_labels():
    app = load_app("field_notes")

    assert [(declaration.name, declaration.label) for declaration in app.navigation_pages()] == [
        ("notes", "notes")
    ]


@pytest.mark.parametrize("named", ["missing", "note", "note_history"])
def test_navigation_takes_only_a_static_top_level_page(monkeypatch, named):
    app = load_app("field_notes")
    monkeypatch.setattr(app, "navigation", [named])

    with pytest.raises(PageRouteError, match="static top-level page"):
        app.navigation_pages()


@pytest.mark.parametrize(
    "prefix, path",
    [
        ("/pages", "/one"),
        ("", "/pages/hidden"),
        ("/uploads", "/one"),
        ("", "/uploads/mine"),
    ],
)
def test_a_router_cannot_take_a_platform_segment(prefix, path):
    app = load_app("field_notes")
    module = ModuleType("druks_field_notes.routes")
    module.router = APIRouter(prefix=prefix)
    module.router.get(path)(lambda: None)

    with pytest.raises(AppRouteConflict, match="serve every app's platform reads"):
        app.get_routers([module])


def test_two_pages_in_one_module_with_one_name_fail():
    declare("one_module_twice", "/", "overview")
    declare("one_module_twice", "/first", "notes")
    declare("one_module_twice", "/second", "notes")

    with pytest.raises(PageRouteError, match="two pages named 'notes'"):
        list_pages_for_app("one_module_twice", "one_module_twice")


def test_static_children_keep_declaration_order():
    declare("tab_order", "/", "overview")
    notes = declare("tab_order", "/notes", "notes")
    declare("tab_order", "/z-last", "last_tab", parent=notes)
    declare("tab_order", "/a-first", "first_tab", parent=notes)

    assert [child.name for child in notes.children] == ["last_tab", "first_tab"]


def test_a_positional_only_parameter_fails():
    declare("positional_only", "/", "overview")

    async def note(note_id, /):
        return Page(title="Note")

    note.__module__ = "positional_only.pages"
    page("/notes/{note_id}")(note)

    with pytest.raises(PageRouteError, match="callable by name"):
        list_pages_for_app("positional_only", "positional_only")


def test_a_catch_all_must_be_the_last_segment():
    declare("late_catch_all", "/", "overview")
    files = declare("late_catch_all", "/files/{rest:path}", "any_file", "rest")
    declare("late_catch_all", "/metadata", "file_metadata", "rest", parent=files)

    with pytest.raises(PageRouteError, match="catch-all is not the last segment"):
        list_pages_for_app("late_catch_all", "late_catch_all")


def test_two_children_of_different_parents_with_one_name_fail():
    declare("shared_child_name", "/", "overview")
    note = declare("shared_child_name", "/notes/{note_id}", "note", "note_id")
    peer = declare("shared_child_name", "/peers/{peer_id}", "peer", "peer_id")
    declare("shared_child_name", "/history", "history", "note_id", parent=note)
    declare("shared_child_name", "/history", "history", "peer_id", parent=peer)

    with pytest.raises(PageRouteError, match="two pages named 'history'"):
        list_pages_for_app("shared_child_name", "shared_child_name")
